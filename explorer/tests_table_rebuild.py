"""Changing the order of a table's columns (explorer/table_rebuild.py): rebuilt in one transaction, everything kept, refused when it would break something."""
import datetime
import json

from django.test import Client
from pgdas.testing import TestCase
from django.urls import reverse

from audit.models import AuditEntry

from .tests import admin_connect
from .tests_admin import AdminFixture, DB

SETUP = """
CREATE TABLE people (id serial PRIMARY KEY, name text NOT NULL, email text UNIQUE, age int CHECK (age >= 0), note text DEFAULT 'none');
CREATE TABLE parents (pid int PRIMARY KEY);
CREATE TABLE kids (id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY, label text NOT NULL, pid int REFERENCES parents (pid),
                   total int GENERATED ALWAYS AS (id * 2) STORED, born date);
CREATE INDEX kids_label_idx ON kids (lower(label));
CREATE FUNCTION touch() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.label := upper(NEW.label); RETURN NEW; END $$;
CREATE TRIGGER kids_touch BEFORE INSERT ON kids FOR EACH ROW EXECUTE FUNCTION touch();
COMMENT ON TABLE kids IS 'the kids';
COMMENT ON COLUMN kids.label IS 'what they are called';
INSERT INTO people (name, email, age) VALUES ('Ann', 'a@x', 30), ('Bob', 'b@x', 41), ('Cy', NULL, NULL);
INSERT INTO parents VALUES (1), (2);
INSERT INTO kids (label, pid, born) VALUES ('x', 1, '2020-01-01'), ('y', 2, NULL);
"""
REORDER = 'ALTER TABLE "public"."{}" REORDER COLUMNS ({})'


class RebuildFixture(AdminFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.staff)
        with admin_connect("postgres") as admin:
            admin.execute(f"CREATE DATABASE {DB}")
        with admin_connect(DB) as conn:
            conn.execute(SETUP)

    def sql(self, query, *params):
        with admin_connect(DB) as conn:
            return conn.execute(query, params).fetchall()

    def columns(self, table):
        return [r[0] for r in self.sql("SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped ORDER BY attnum", table)]

    def send(self, name, statement, **extra):
        body = {"sql": statement, "db": DB, **extra}
        return self.client.post(reverse(name, args=[self.server.pk]), json.dumps(body), content_type="application/json")

    def rebuild(self, table, order, confirm=None):
        statement = REORDER.format(table, ", ".join('"%s"' % c for c in order))
        preview = self.send("write_preview", statement)
        self.assertEqual(preview.status_code, 200, preview.content)
        data = preview.json()
        return statement, data, self.send("write_execute", statement, fingerprint=data["fingerprint"], confirm=table if confirm is None else confirm)


class ReorderTests(RebuildFixture):
    def test_preview_changes_nothing_and_asks_for_the_table_name(self):
        statement, data, _ = self.rebuild("people", ["note", "age", "email", "name", "id"])
        self.assertEqual((data["confirm_word"], data["confirm_prompt"], data["button"], data["title"]), ("people", "Type the table name to confirm", "Rebuild table", "REBUILD TABLE?"))
        self.assertEqual(data["fingerprint"]["rows"], 3)
        self.assertTrue(any("locked completely" in n["text"] for n in data["notes"]))
        self.assertEqual(AuditEntry.objects.filter(action="alter").count(), 1)               # only the executed one below, not the preview

    def test_rebuilds_in_the_new_order_and_keeps_rows_indexes_constraints_and_counters(self):
        before_rows = {r[0]: r for r in self.sql("SELECT id, name, email, age, note FROM people")}
        before_defs = self.sql("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'people' ORDER BY 1")
        before_cons = self.sql("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'people'::regclass AND contype <> 'n' ORDER BY 1")
        order = ["note", "age", "email", "name", "id"]
        _, _, response = self.rebuild("people", order)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.columns("people"), order)
        self.assertEqual({r[0]: r for r in self.sql("SELECT id, name, email, age, note FROM people")}, before_rows)
        self.assertEqual(self.sql("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'people' ORDER BY 1"), before_defs)
        self.assertEqual(self.sql("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'people'::regclass AND contype <> 'n' ORDER BY 1"), before_cons)
        self.assertEqual(self.sql("INSERT INTO people (name) VALUES ('Di') RETURNING id, note"), [(4, "none")])        # the serial counter went on
        self.assertEqual(self.sql("SELECT pg_get_serial_sequence('people', 'id')")[0][0], "public.people_id_seq")
        entry = AuditEntry.objects.filter(action="alter").latest("pk")
        self.assertTrue(entry.ok)
        self.assertEqual(entry.payload["after_order"], order)
        self.assertIn("CREATE TABLE", entry.payload["before"][0]["sql"])

    def test_identity_generated_columns_trigger_comments_foreign_key_and_index_survive(self):
        order = ["born", "total", "pid", "label", "id"]
        _, _, response = self.rebuild("kids", order)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.columns("kids"), order)
        self.assertEqual(self.sql("SELECT id, label, pid, total, born FROM kids ORDER BY id"), [(1, "X", 1, 2, datetime.date(2020, 1, 1)), (2, "Y", 2, 4, None)])     # (the trigger upper-cased them when they were first inserted; the copy did not run it again)
        self.assertEqual(self.sql("INSERT INTO kids (label) VALUES ('z') RETURNING id, label, total"), [(3, "Z", 6)])     # identity went on, trigger fired, generated column works
        self.assertEqual(self.sql("SELECT obj_description('kids'::regclass, 'pg_class'), col_description('kids'::regclass, 4)")[0], ("the kids", "what they are called"))
        self.assertEqual(len(self.sql("SELECT 1 FROM pg_indexes WHERE indexname = 'kids_label_idx'")), 1)
        self.assertEqual(len(self.sql("SELECT 1 FROM pg_constraint WHERE conrelid = 'kids'::regclass AND contype = 'f'")), 1)
        self.assertEqual(len(self.sql("SELECT 1 FROM pg_trigger WHERE tgrelid = 'kids'::regclass AND NOT tgisinternal")), 1)

    def test_a_wrong_or_missing_name_changes_nothing(self):
        for typed in ("", "People", "peopl"):
            with self.subTest(typed=typed):
                _, _, response = self.rebuild("people", ["name", "id", "email", "age", "note"], confirm=typed)
                self.assertEqual(response.status_code, 400)
                self.assertIn("Type the table name exactly", response.json()["error"])
        self.assertEqual(self.columns("people"), ["id", "name", "email", "age", "note"])

    def test_the_same_order_and_a_list_that_is_not_every_column_are_refused(self):
        for order, message in ((["id", "name", "email", "age", "note"], "already have"), (["id", "name"], "every column")):
            with self.subTest(order=order):
                response = self.send("write_preview", REORDER.format("people", ", ".join('"%s"' % c for c in order)))
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json()["error"])

    def test_a_table_other_tables_depend_on_is_refused_with_the_reason(self):
        response = self.send("write_preview", REORDER.format("parents", '"pid"'))
        self.assertEqual(response.status_code, 400)                                           # one column: already in order
        with admin_connect(DB) as conn:
            conn.execute("ALTER TABLE parents ADD COLUMN extra int; CREATE VIEW people_view AS SELECT * FROM people")
        for table, order, needle in (("parents", ["extra", "pid"], "kids"), ("people", ["name", "id", "email", "age", "note"], "people_view")):
            with self.subTest(table=table):
                response = self.send("write_preview", REORDER.format(table, ", ".join('"%s"' % c for c in order)))
                self.assertEqual(response.status_code, 400)
                self.assertIn("cannot be rebuilt safely", response.json()["error"])
                self.assertIn(needle, response.json()["error"])
        self.assertEqual(self.columns("parents"), ["pid", "extra"])

    def test_a_failed_rebuild_leaves_the_table_as_it_was(self):
        statement = REORDER.format("people", ", ".join('"%s"' % c for c in ["note", "age", "email", "name", "id"]))
        data = self.send("write_preview", statement).json()
        with admin_connect(DB) as conn:
            conn.execute("INSERT INTO people (name) VALUES ('late')")                        # the table changed after it was checked
        response = self.send("write_execute", statement, fingerprint=data["fingerprint"], confirm="people")
        self.assertEqual(response.status_code, 400)
        self.assertIn("changed since you checked", response.json()["error"])
        self.assertEqual(self.columns("people"), ["id", "name", "email", "age", "note"])
        self.assertEqual(self.sql("SELECT count(*) FROM people")[0][0], 4)

    def test_only_staff_can_use_it(self):
        from django.contrib.auth import get_user_model
        plain = Client()
        plain.force_login(get_user_model().objects.create_user("pat", password="pw-12345-abc"))
        response = plain.post(reverse("write_preview", args=[self.server.pk]),
                              json.dumps({"sql": REORDER.format("people", '"name", "id", "email", "age", "note"'), "db": DB}), content_type="application/json")
        self.assertIn(response.status_code, (403, 404))
