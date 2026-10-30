"""Deleting rows (with the ZAP confirmation), recording them as JSON in the audit log, and re-inserting them."""
import json
from unittest.mock import patch

import psycopg
from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from audit.models import AuditEntry
from servers.models import ServerConnection

from . import pg, writes
from . import tests as shared          # the PostgreSQL fixture (imported as a module so its tests are not run twice)


class ParseDeleteTests(SimpleTestCase):
    def parse(self, text):
        return writes.parse_delete(text)

    def test_accepts_plain_deletes_and_finds_the_table(self):
        cases = {
            "DELETE FROM t WHERE x = 1": ("t", "t", True),
            "delete from public.t where x = 1;": ("public.t", "public.t", True),
            'DELETE FROM "My Table" WHERE "a b" = 2': ('"My Table"', '"My Table"', True),
            'DELETE FROM  s . "T t"  WHERE 1=1': ('s."T t"', 's."T t"', True),
            "DELETE FROM ONLY t WHERE x = 1": ("t", "t", True),
            "DELETE FROM t": ("t", "t", False),
            "DELETE FROM t AS a USING u WHERE a.id = u.id": ("t", "a", True),
            "DELETE FROM t a WHERE a.id = 1": ("t", "a", True),
            "DELETE FROM t USING u WHERE t.id = u.id": ("t", "t", True),
            "  -- clear it\n DELETE /* old */ FROM t WHERE x = 1  ": ("t", "t", True),
            "DELETE FROM t WHERE name = 'a ) b'": ("t", "t", True),      # a parenthesis inside a string is not a bracket
            "DELETE FROM t WHERE id IN (SELECT id FROM u WHERE (x > 1))": ("t", "t", True),
        }
        for text, (table, target, has_where) in cases.items():
            with self.subTest(text=text):
                parsed = self.parse(text)
                self.assertEqual((parsed["table_ref"], parsed["target"], parsed["has_where"]), (table, target, has_where))
                self.assertNotIn("--", parsed["statement"])                # comments are removed
                self.assertFalse(parsed["statement"].endswith(";"))

    def test_refuses_anything_else_with_a_plain_message(self):
        cases = {
            "": "Enter a DELETE",
            "   ;  ": "Enter a DELETE",
            "SELECT 1": "Only DELETE",
            "DROP TABLE t": "Only DELETE",
            "UPDATE t SET x = 1": "Only DELETE",
            "TRUNCATE t": "Only DELETE",
            "DELETE FROM t; DROP TABLE u": "one DELETE statement",
            "DELETE FROM t WHERE x = 1; DELETE FROM u": "one DELETE statement",
            "WITH x AS (SELECT 1) DELETE FROM t": "WITH clause",
            "DELETE FROM t WHERE x = 1 RETURNING *": "RETURNING",
            "DELETE FROM t WHERE x = 1) SELECT 1 --": "parentheses",
            "DELETE FROM t WHERE (x = 1": "parentheses",
            "DELETE t": "Write it as DELETE FROM",
        }
        for text, message in cases.items():
            with self.subTest(text=text), self.assertRaisesRegex(pg.PgError, message):
                self.parse(text)


class WriteFixture(shared.PgFixture):
    """Fresh, changeable tables for every test, in the scratch PostgreSQL database."""

    def setUp(self):
        super().setUp()
        with shared.admin_connect(shared.TARGET["NAME"]) as conn:
            for statement in (
                "DROP SCHEMA IF EXISTS pgdeck_w CASCADE", "CREATE SCHEMA pgdeck_w",
                """CREATE TABLE pgdeck_w.parent (id int PRIMARY KEY, name text NOT NULL, data jsonb, amount numeric(12, 4),
                       born date, seen timestamptz, blob bytea, tags text[], note text)""",
                """INSERT INTO pgdeck_w.parent
                       SELECT g, 'parent ' || g, jsonb_build_object('n', g, 'ok', g % 2 = 0), g * 1.2345, DATE '2020-01-01' + g,
                              TIMESTAMPTZ '2024-05-06 07:08:09+00' + g * interval '1 hour', decode(lpad(to_hex(g), 2, '0'), 'hex'),
                              ARRAY['a' || g, 'it''s'], CASE WHEN g % 3 = 0 THEN NULL ELSE 'note ' || g END
                       FROM generate_series(1, 10) g""",
                "CREATE TABLE pgdeck_w.child (id serial PRIMARY KEY, parent_id int REFERENCES pgdeck_w.parent(id) ON DELETE CASCADE, label text)",
                "INSERT INTO pgdeck_w.child (parent_id, label) SELECT p, 'child ' || p || c FROM generate_series(1, 10) p, generate_series(1, 2) c",
                """CREATE TABLE pgdeck_w.ident (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, label text,
                       doubled int GENERATED ALWAYS AS (length(label) * 2) STORED)""",
                "INSERT INTO pgdeck_w.ident (label) SELECT 'label ' || g FROM generate_series(1, 5) g",
                "CREATE TABLE pgdeck_w.logged (id int PRIMARY KEY, what text)",
                "INSERT INTO pgdeck_w.logged VALUES (1, 'a'), (2, 'b')",
                "CREATE TABLE pgdeck_w.log (id serial PRIMARY KEY, msg text)",
                """CREATE FUNCTION pgdeck_w.note_delete() RETURNS trigger LANGUAGE plpgsql AS
                       $$ BEGIN INSERT INTO pgdeck_w.log (msg) VALUES ('deleted ' || OLD.id); RETURN OLD; END $$""",
                "CREATE TRIGGER logged_delete AFTER DELETE ON pgdeck_w.logged FOR EACH ROW EXECUTE FUNCTION pgdeck_w.note_delete()",
                "CREATE VIEW pgdeck_w.parent_view AS SELECT id, name FROM pgdeck_w.parent",
            ):
                conn.execute(statement)
        self.staff = get_user_model().objects.create_user("zed", password="pw-12345-abc", is_staff=True)
        self.server = shared.make_server(allow_writes=True)
        self.db = shared.TARGET["NAME"]

    def query(self, text, *params):
        with shared.admin_connect(self.db) as conn:
            return conn.execute(text, params).fetchall()

    def count(self, table):
        return self.query(f"SELECT count(*) FROM pgdeck_w.{table}")[0][0]

    def snapshot(self, table):
        """Every row as JSON, in key order: used to prove a re-insert restores exactly what was deleted."""
        return [row[0] for row in self.query(f"SELECT to_jsonb(t) FROM pgdeck_w.{table} t ORDER BY id")]

    def delete(self, text, count, effects=None, confirm="ZAP"):
        return writes.execute_delete(self.server, self.db, text, count, effects or [], confirm, self.staff)


class PreviewTests(WriteFixture, TestCase):
    def delete(self, text, count, effects=None, confirm="ZAP"):
        return writes.execute_delete(self.server, self.db, text, count, effects or [], confirm, self.staff)

    def test_preview_counts_the_rows_and_changes_nothing(self):
        preview = writes.preview_delete(self.server, self.db, "DELETE FROM pgdeck_w.parent WHERE id <= 3 AND id <> 99")
        self.assertEqual((preview["schema"], preview["table"], preview["count"], preview["has_where"]), ("pgdeck_w", "parent", 3, True))
        self.assertEqual(len(preview["sample"]), 3)
        self.assertIn("id", preview["columns"])
        self.assertEqual(self.count("parent"), 10)                          # the preview is always rolled back
        self.assertFalse(AuditEntry.objects.exists())                       # and records nothing

    def test_columns_and_rows_follow_the_tables_own_column_order(self):
        # PostgreSQL's JSON type sorts keys by length ("id", "note", "name"...). People expect the table's order.
        preview = writes.preview_delete(self.server, self.db, "DELETE FROM pgdeck_w.parent WHERE id = 3")
        table_order = ["id", "name", "data", "amount", "born", "seen", "blob", "tags", "note"]
        self.assertEqual(preview["columns"], table_order)
        self.assertEqual(list(preview["sample"][0]), table_order)
        entry = AuditEntry.objects.get(pk=self.delete("DELETE FROM pgdeck_w.parent WHERE id = 3", 1,
                                                      [{"table": "pgdeck_w.child", "inserted": 0, "updated": 0, "deleted": 2}])["entry_id"])
        self.assertEqual(entry.payload["columns"], table_order)
        self.assertEqual(list(entry.payload["rows"][0]), table_order)           # also in the stored (and downloaded) JSON
        self.assertLess(entry.payload_preview.index('"name"'), entry.payload_preview.index('"note"'))

    def test_preview_shows_only_a_sample_but_counts_everything(self):
        preview = writes.preview_delete(self.server, self.db, "DELETE FROM pgdeck_w.parent")
        self.assertEqual((preview["count"], len(preview["sample"]), preview["has_where"]), (10, writes.SAMPLE_ROWS, False))
        self.assertEqual(self.count("parent"), 10)

    def test_preview_warns_about_cascades_and_triggers(self):
        cascade = writes.preview_delete(self.server, self.db, "DELETE FROM pgdeck_w.parent WHERE id = 1")
        self.assertEqual(cascade["side_effects"], [{"table": "pgdeck_w.child", "inserted": 0, "updated": 0, "deleted": 2}])
        trigger = writes.preview_delete(self.server, self.db, "DELETE FROM pgdeck_w.logged WHERE id = 1")
        self.assertEqual(trigger["side_effects"], [{"table": "pgdeck_w.log", "inserted": 1, "updated": 0, "deleted": 0}])
        plain = writes.preview_delete(self.server, self.db, "DELETE FROM pgdeck_w.ident WHERE id = 1")
        self.assertEqual(plain["side_effects"], [])
        self.assertEqual((self.count("child"), self.count("log")), (20, 0))

    def test_preview_with_alias_and_using(self):
        preview = writes.preview_delete(self.server, self.db,
            "DELETE FROM pgdeck_w.parent AS p USING pgdeck_w.child c WHERE c.parent_id = p.id AND c.label = 'child 51'")
        self.assertEqual((preview["count"], preview["sample"][0]["id"]), (1, 5))
        self.assertNotIn("label", preview["columns"])                       # only the deleted table's columns, not the USING table's

    def test_preview_of_nothing_matching(self):
        preview = writes.preview_delete(self.server, self.db, "DELETE FROM pgdeck_w.parent WHERE id = 12345")
        self.assertEqual((preview["count"], preview["sample"], preview["columns"]), (0, [], []))

    def test_preview_refuses_what_it_cannot_record(self):
        with override_settings(MAX_DELETE_ROWS=3), self.assertRaisesRegex(pg.PgError, "more than 3 rows"):
            writes.preview_delete(self.server, self.db, "DELETE FROM pgdeck_w.parent")
        self.assertEqual(self.count("parent"), 10)

    def test_preview_errors_are_plain(self):
        for text, message in (("DELETE FROM pgdeck_w.nope WHERE id = 1", "was not found"),
                              ("DELETE FROM pgdeck_w.parent_view WHERE id = 1", "ordinary tables"),
                              ("DELETE FROM pgdeck_w.parent WHERE nocolumn = 1", "nocolumn")):
            with self.subTest(text=text), self.assertRaisesRegex(pg.PgError, message):
                writes.preview_delete(self.server, self.db, text)


class ExecuteTests(WriteFixture, TestCase):
    def test_only_the_exact_word_zap_confirms(self):
        for wrong in ("zap", "Zap", "ZAp", " ZAP", "ZAP ", "ZAPP", "ZA", "", None, "ZAP\n"):
            with self.subTest(confirm=wrong), self.assertRaisesRegex(pg.PgError, "Type ZAP in capital letters"):
                self.delete("DELETE FROM pgdeck_w.ident WHERE id = 1", 1, confirm=wrong)
        self.assertEqual(self.count("ident"), 5)
        self.assertFalse(AuditEntry.objects.exists())

    def test_confirmed_delete_removes_the_rows_and_records_them_as_json(self):
        before = self.query("SELECT to_jsonb(p) FROM pgdeck_w.parent p WHERE id IN (3, 4) ORDER BY id")
        result = self.delete("DELETE FROM pgdeck_w.logged WHERE id = 2", 1, [{"table": "pgdeck_w.log", "inserted": 1, "updated": 0, "deleted": 0}])
        self.assertEqual((result["count"], result["schema"], result["table"]), (1, "pgdeck_w", "logged"))
        result = self.delete("DELETE FROM pgdeck_w.parent WHERE id IN (3, 4)",
                             2, [{"table": "pgdeck_w.child", "inserted": 0, "updated": 0, "deleted": 4}])
        self.assertEqual(self.count("parent"), 8)
        entry = AuditEntry.objects.get(pk=result["entry_id"])
        self.assertEqual((entry.action, entry.ok, entry.rows, entry.username, entry.server_id), ("delete", True, 2, "zed", self.server.pk))
        payload = entry.payload
        self.assertEqual((payload["kind"], payload["database"], payload["schema"], payload["table"], payload["count"]),
                         ("delete", self.db, "pgdeck_w", "parent", 2))
        self.assertEqual(payload["no_where"], False)
        self.assertEqual(payload["side_effects"][0]["table"], "pgdeck_w.child")
        by_id = sorted(payload["rows"], key=lambda row: row["id"])
        self.assertEqual(by_id, [row[0] for row in before])                   # the JSON is exactly what was deleted
        self.assertIn("id", payload["columns"])
        row3 = by_id[0]
        self.assertEqual((row3["id"], row3["name"], row3["note"]), (3, "parent 3", None))
        self.assertEqual(row3["data"], {"n": 3, "ok": False})                  # nested JSON stays JSON
        self.assertEqual(row3["tags"], ["a3", "it's"])                         # arrays stay arrays
        self.assertEqual(row3["amount"], 3.7035)                                # numbers stay numbers
        self.assertTrue(row3["born"].startswith("2020-01-04"))
        self.assertTrue(row3["blob"].startswith("\\x"))
        json.dumps(payload)                                                     # the whole record is valid JSON

    def test_the_data_must_still_be_what_was_confirmed(self):
        with self.assertRaisesRegex(pg.PgError, "changed since you checked"):
            self.delete("DELETE FROM pgdeck_w.ident WHERE id <= 3", 2)         # 3 match now, 2 were confirmed
        with self.assertRaisesRegex(pg.PgError, "changed since you checked"):
            self.delete("DELETE FROM pgdeck_w.parent WHERE id = 1", 1)         # the cascade into child was not confirmed
        with self.assertRaisesRegex(pg.PgError, "changed since you checked"):
            self.delete("DELETE FROM pgdeck_w.parent WHERE id = 1", 1, [{"table": "pgdeck_w.child", "inserted": 0, "updated": 0, "deleted": 99}])
        self.assertEqual((self.count("ident"), self.count("parent"), self.count("child")), (5, 10, 20))
        self.assertFalse(AuditEntry.objects.exists())

    def test_nothing_to_delete_is_refused(self):
        with self.assertRaisesRegex(pg.PgError, "nothing to delete"):
            self.delete("DELETE FROM pgdeck_w.parent WHERE id = 12345", 0)
        self.assertFalse(AuditEntry.objects.exists())

    def test_no_audit_record_means_no_delete(self):
        with patch("explorer.writes.record", side_effect=RuntimeError("audit store is full")):
            with self.assertRaisesRegex(pg.PgError, "could not save the audit record"):
                self.delete("DELETE FROM pgdeck_w.ident WHERE id = 1", 1)
        self.assertEqual(self.count("ident"), 5)                               # rolled back: nothing was deleted

    def test_a_refused_commit_is_marked_in_the_record(self):
        # Deferred foreign key checks run at commit; make one fail there.
        with shared.admin_connect(self.db) as conn:
            conn.execute("CREATE TABLE pgdeck_w.keeper (id int PRIMARY KEY)")
            conn.execute("INSERT INTO pgdeck_w.keeper VALUES (1), (2)")
            conn.execute("CREATE TABLE pgdeck_w.holder (k int REFERENCES pgdeck_w.keeper(id) DEFERRABLE INITIALLY DEFERRED)")
            conn.execute("INSERT INTO pgdeck_w.holder VALUES (1)")
        with self.assertRaisesRegex(pg.PgError, "did not accept the delete"):
            self.delete("DELETE FROM pgdeck_w.keeper WHERE id = 1", 1)
        self.assertEqual(self.query("SELECT count(*) FROM pgdeck_w.keeper")[0][0], 2)
        entry = AuditEntry.objects.get()
        self.assertFalse(entry.ok)
        self.assertIn("not committed", entry.detail)
        self.assertFalse(entry.can_reinsert)                                    # nothing was deleted, so nothing to put back


class ReinsertTests(WriteFixture, TestCase):
    def deleted_entry(self, text, count, effects=None):
        return AuditEntry.objects.get(pk=self.delete(text, count, effects)["entry_id"])

    def test_reinsert_restores_the_rows_exactly_once(self):
        before = self.snapshot("parent")
        entry = self.deleted_entry("DELETE FROM pgdeck_w.parent WHERE id IN (3, 6, 9)", 3,
                                   [{"table": "pgdeck_w.child", "inserted": 0, "updated": 0, "deleted": 6}])
        self.assertTrue(entry.can_reinsert)
        self.assertEqual(self.count("parent"), 7)
        result = writes.reinsert(entry, self.staff)
        self.assertEqual((result["count"], result["schema"], result["table"]), (3, "pgdeck_w", "parent"))
        self.assertEqual(self.snapshot("parent"), before)                       # every value, type and NULL is back
        self.assertEqual(self.count("child"), 14)                               # cascaded rows are not restored (and are said to be lost)
        entry.refresh_from_db()
        self.assertEqual((entry.restored_by, entry.can_reinsert), ("zed", False))
        self.assertIsNotNone(entry.restored_at)
        restore = AuditEntry.objects.get(action="delete.restore")
        self.assertEqual((restore.rows, restore.payload["restored_entry"], restore.payload["count"]), (3, entry.pk, 3))
        with self.assertRaisesRegex(pg.PgError, "already re-inserted"):
            writes.reinsert(entry, self.staff)
        self.assertEqual(self.count("parent"), 10)

    def test_identity_always_and_generated_columns_are_handled(self):
        before = self.snapshot("ident")
        entry = self.deleted_entry("DELETE FROM pgdeck_w.ident WHERE id IN (2, 4)", 2)
        self.assertEqual([row["doubled"] for row in entry.payload["rows"]], [14, 14])   # captured, but not inserted back by hand
        writes.reinsert(entry, self.staff)
        self.assertEqual(self.snapshot("ident"), before)                        # same ids (identity), generated values recomputed

    def test_a_clash_with_existing_data_changes_nothing(self):
        entry = self.deleted_entry("DELETE FROM pgdeck_w.logged WHERE id = 1", 1, [{"table": "pgdeck_w.log", "inserted": 1, "updated": 0, "deleted": 0}])
        with shared.admin_connect(self.db) as conn:
            conn.execute("INSERT INTO pgdeck_w.logged VALUES (1, 'someone put this here')")
        with self.assertRaisesRegex(pg.PgError, "duplicate key"):
            writes.reinsert(entry, self.staff)
        entry.refresh_from_db()
        self.assertIsNone(entry.restored_at)                                    # still restorable once the clash is resolved
        self.assertFalse(AuditEntry.objects.filter(action="delete.restore").exists())
        self.assertEqual(self.query("SELECT what FROM pgdeck_w.logged WHERE id = 1")[0][0], "someone put this here")

    def test_a_table_that_changed_shape(self):
        entry = self.deleted_entry("DELETE FROM pgdeck_w.parent WHERE id = 10", 1, [{"table": "pgdeck_w.child", "inserted": 0, "updated": 0, "deleted": 2}])
        with shared.admin_connect(self.db) as conn:
            conn.execute("ALTER TABLE pgdeck_w.parent DROP COLUMN note")
        with self.assertRaisesRegex(pg.PgError, "no longer has the column note"):
            writes.reinsert(entry, self.staff)
        with shared.admin_connect(self.db) as conn:
            conn.execute("DROP TABLE pgdeck_w.child")
            conn.execute("DROP TABLE pgdeck_w.parent CASCADE")        # (the view on it goes too)
        with self.assertRaisesRegex(pg.PgError, "was not found"):
            writes.reinsert(entry, self.staff)

    def test_what_may_not_be_restored(self):
        entry = self.deleted_entry("DELETE FROM pgdeck_w.ident WHERE id = 1", 1)
        self.server.allow_writes = False
        self.server.save()
        entry.refresh_from_db()
        with self.assertRaisesRegex(pg.PgError, "Changes are not allowed"):
            writes.reinsert(entry, self.staff)
        self.server.delete()
        entry.refresh_from_db()
        with self.assertRaisesRegex(pg.PgError, "no longer saved"):
            writes.reinsert(entry, self.staff)
        read = AuditEntry.objects.create(username="zed", action="sql.run", detail="SELECT 1")
        with self.assertRaisesRegex(pg.PgError, "not a recorded delete"):
            writes.reinsert(read, self.staff)


class DeleteEndpointTests(WriteFixture, TestCase):
    def post(self, name, data, client=None, **extra):
        return (client or self.client).post(reverse(name, args=[self.server.pk]), json.dumps(data), content_type="application/json", **extra)

    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)

    def test_preview_endpoint(self):
        response = self.post("delete_preview", {"sql": "DELETE FROM pgdeck_w.parent WHERE id < 4", "db": self.db})
        body = response.json()
        self.assertEqual((response.status_code, body["ok"], body["count"], body["table"]), (200, True, 3, "parent"))
        self.assertEqual(len(body["sample"]), 3)
        self.assertEqual(self.count("parent"), 10)
        bad = self.post("delete_preview", {"sql": "SELECT 1", "db": self.db})
        self.assertEqual((bad.status_code, bad.json()["ok"]), (400, False))
        self.assertIn("Only DELETE", bad.json()["error"])
        self.assertEqual(self.post("delete_preview", "not an object").status_code, 400)
        self.assertEqual(self.client.generic("POST", reverse("delete_preview", args=[self.server.pk]), "{broken", "application/json").status_code, 400)

    def test_execute_endpoint_requires_the_word_and_records_everything(self):
        data = {"sql": "DELETE FROM pgdeck_w.ident WHERE id IN (1, 2)", "db": self.db, "expected_count": 2, "expected_side_effects": []}
        refused = self.post("delete_execute", {**data, "confirm": "zap"})
        self.assertEqual((refused.status_code, refused.json()["ok"]), (400, False))
        self.assertIn("capital letters", refused.json()["error"])
        self.assertEqual(self.count("ident"), 5)
        failed = AuditEntry.objects.get(action="delete")
        self.assertFalse(failed.ok)                                              # a refused attempt is in the log too
        done = self.post("delete_execute", {**data, "confirm": "ZAP"})
        self.assertEqual((done.status_code, done.json()["ok"], done.json()["count"]), (200, True, 2))
        self.assertEqual(self.count("ident"), 3)
        entry = AuditEntry.objects.get(pk=done.json()["entry_id"])
        self.assertTrue(entry.ok and entry.can_reinsert)

    def test_execute_endpoint_rejects_a_stale_count_and_junk(self):
        data = {"sql": "DELETE FROM pgdeck_w.ident", "db": self.db, "expected_side_effects": [], "confirm": "ZAP"}
        stale = self.post("delete_execute", {**data, "expected_count": 4})
        self.assertIn("changed since you checked", stale.json()["error"])
        for junk in ({**data}, {**data, "expected_count": "many"}):
            self.assertEqual(self.post("delete_execute", junk).status_code, 400)
        self.assertEqual(self.count("ident"), 5)

    def test_who_may_use_it(self):
        payload = {"sql": "DELETE FROM pgdeck_w.ident WHERE id = 1", "db": self.db}
        # a signed-in user who is not staff
        plain = get_user_model().objects.create_user("pat", password="pw-12345-abc")
        other = Client()
        other.force_login(plain)
        for name in ("delete_preview", "delete_execute"):
            response = self.post(name, {**payload, "expected_count": 1, "confirm": "ZAP"}, client=other)
            self.assertEqual(response.status_code, 403, name)
            self.assertIn("Only staff", response.json()["error"])
        # staff, but the server has not been opened for deleting
        self.server.allow_writes = False
        self.server.save()
        for name in ("delete_preview", "delete_execute"):
            response = self.post(name, {**payload, "expected_count": 1, "confirm": "ZAP"})
            self.assertEqual(response.status_code, 403, name)
            self.assertIn("switched off", response.json()["error"])
        # not signed in at all
        for name in ("delete_preview", "delete_execute"):
            self.assertEqual(self.post(name, payload, client=Client()).status_code, 302, name)
        self.assertEqual(self.count("ident"), 5)

    def test_get_is_not_allowed_and_csrf_is_enforced(self):
        url = reverse("delete_execute", args=[self.server.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.staff)
        response = strict.post(url, json.dumps({"sql": "DELETE FROM pgdeck_w.ident", "confirm": "ZAP", "expected_count": 5}),
                               content_type="application/json")
        self.assertEqual(response.status_code, 403)                              # no CSRF token: refused before anything runs
        self.assertEqual(self.count("ident"), 5)


class PagesTests(WriteFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)

    def test_sql_page_has_the_delete_popup_only_for_staff_on_an_allowed_server(self):
        page = self.client.get(reverse("sql_runner", args=[self.server.pk]))
        for text in ("deletes allowed", 'data-can-delete="1"', 'id="deleteModal"', "There is no rollback", "delete_flow.js",
                     'id="deleteConfirm"', "capital letters", reverse("delete_preview", args=[self.server.pk])):
            self.assertContains(page, text)
        self.assertContains(page, 'data-confirm-word="ZAP"')
        self.server.allow_writes = False
        self.server.save()
        off = self.client.get(reverse("sql_runner", args=[self.server.pk]))
        for text in ("deleteModal", "delete_flow.js", "deletes allowed", "data-can-delete"):
            self.assertNotContains(off, text)
        self.assertContains(off, "read-only")
        self.server.allow_writes = True
        self.server.save()
        plain = get_user_model().objects.create_user("pat", password="pw-12345-abc")
        self.client.force_login(plain)
        self.assertNotContains(self.client.get(reverse("sql_runner", args=[self.server.pk])), "deleteModal")

    def test_a_delete_typed_into_the_normal_form_still_cannot_write(self):
        response = self.client.post(reverse("sql_runner", args=[self.server.pk]),
                                    {"sql": "DELETE FROM pgdeck_w.ident WHERE id = 1", "db": self.db})
        self.assertContains(response, "read-only transaction")
        self.assertContains(response, "needs JavaScript")                       # staff on an allowed server: told how to proceed
        self.assertEqual(self.count("ident"), 5)                                 # the plain path stays read-only everywhere
        plain = get_user_model().objects.create_user("pat", password="pw-12345-abc")
        self.client.force_login(plain)
        other = self.client.post(reverse("sql_runner", args=[self.server.pk]),
                                 {"sql": "DELETE FROM pgdeck_w.ident WHERE id = 1", "db": self.db})
        self.assertContains(other, "Only staff users can delete rows")
        self.assertEqual(self.count("ident"), 5)

    def test_the_hint_after_a_blocked_delete(self):
        self.server.allow_writes = False
        self.server.save()
        response = self.client.post(reverse("sql_runner", args=[self.server.pk]),
                                    {"sql": "DELETE FROM pgdeck_w.ident WHERE id = 1", "db": self.db})
        self.assertContains(response, "Deleting rows is switched off here")
        self.assertEqual(self.count("ident"), 5)

    def test_audit_log_shows_the_json_record_and_offers_reinsert(self):
        entry = AuditEntry.objects.get(pk=self.post_delete()["entry_id"])
        page = self.client.get(reverse("audit_list"))
        for text in ("JSON record", "1 row from pgdeck_w.ident", f"Re-insert 1 row", "Download JSON", "reinsertModal",
                     '&quot;kind&quot;: &quot;delete&quot;', "&quot;label&quot;: &quot;label 1&quot;"):
            self.assertContains(page, text)
        self.assertContains(page, reverse("audit_reinsert", args=[entry.pk]))

    def post_delete(self):
        return self.client.post(reverse("delete_execute", args=[self.server.pk]), json.dumps(
            {"sql": "DELETE FROM pgdeck_w.ident WHERE id = 1", "db": self.db, "expected_count": 1, "expected_side_effects": [],
             "confirm": "ZAP"}), content_type="application/json").json()

    def test_reinsert_from_the_audit_log(self):
        entry = AuditEntry.objects.get(pk=self.post_delete()["entry_id"])
        self.assertEqual(self.client.get(reverse("audit_reinsert", args=[entry.pk])).status_code, 405)
        response = self.client.post(reverse("audit_reinsert", args=[entry.pk]), {"page": "1"}, follow=True)
        self.assertContains(response, "Re-inserted 1 row into pgdeck_w.ident")
        self.assertEqual(self.count("ident"), 5)
        self.assertContains(response, "re-inserted")
        self.assertNotContains(response, "Re-insert 1 row")                      # no second button once restored
        again = self.client.post(reverse("audit_reinsert", args=[entry.pk]), follow=True)
        self.assertContains(again, "already re-inserted")
        self.assertEqual(self.count("ident"), 5)
        self.assertTrue(AuditEntry.objects.filter(action="delete.restore", ok=False).exists())    # the failed attempt is logged

    def test_json_download(self):
        entry = AuditEntry.objects.get(pk=self.post_delete()["entry_id"])
        response = self.client.get(reverse("audit_json", args=[entry.pk]))
        self.assertEqual(response["Content-Type"], "application/json; charset=utf-8")
        self.assertIn(f"pgdeck-audit-{entry.pk}.json", response["Content-Disposition"])
        data = json.loads(response.content)
        self.assertEqual((data["kind"], data["count"], data["rows"][0]["id"]), ("delete", 1, 1))
        plain_entry = AuditEntry.objects.create(username="zed", action="sql.run")
        self.assertEqual(self.client.get(reverse("audit_json", args=[plain_entry.pk])).status_code, 404)

    def test_audit_actions_are_staff_only(self):
        entry = AuditEntry.objects.get(pk=self.post_delete()["entry_id"])
        self.client.force_login(get_user_model().objects.create_user("pat", password="pw-12345-abc"))
        for response in (self.client.get(reverse("audit_json", args=[entry.pk])),
                         self.client.post(reverse("audit_reinsert", args=[entry.pk])),
                         self.client.get(reverse("audit_list"))):
            self.assertEqual(response.status_code, 302)
        self.assertEqual(self.count("ident"), 4)                                 # nothing was restored

    def test_a_long_record_shows_a_preview_and_a_pointer_to_the_download(self):
        entry = AuditEntry.objects.create(username="zed", action="delete", payload={
            "kind": "delete", "schema": "s", "table": "t", "count": 25, "rows": [{"id": i} for i in range(25)]})
        self.assertIn("15 more rows in the downloadable JSON", entry.payload_preview)
        self.assertNotIn('"id": 24', entry.payload_preview)
        self.assertIn('"id": 9', entry.payload_preview)

    def test_turning_deletes_on_or_off_is_audited(self):
        url = reverse("server_edit", args=[self.server.pk])
        form = {"name": "test", "host": self.server.host, "port": self.server.port, "username": self.server.username,
                "password": "", "sslmode": "prefer"}
        self.client.post(url, form)                                              # the checkbox is left out: switched off
        self.assertTrue(AuditEntry.objects.filter(action="server.edit", detail__endswith="deleting rows switched OFF").exists())
        self.client.post(url, {**form, "allow_writes": "on"})
        self.assertTrue(AuditEntry.objects.filter(action="server.edit", detail__endswith="deleting rows switched ON").exists())
        self.client.post(url, {**form, "allow_writes": "on"})                    # unchanged: no extra note
        self.assertEqual(AuditEntry.objects.filter(action="server.edit", detail__contains="switched").count(), 2)

    def test_the_popup_script_checks_the_word_and_writes_values_as_text(self):
        script = open("static/delete_flow.js", encoding="utf-8").read()
        self.assertIn("confirmInput.value !== CONFIRM", script)                    # exact, case-sensitive
        self.assertIn("goButton.disabled = confirmInput.value !== CONFIRM", script)
        self.assertNotIn("innerHTML = row", script)
        self.assertNotIn(".innerHTML = preview", script)
        self.assertIn("textContent", script)
        self.assertIn("expected_count", script)
        self.assertIn("X-CSRFToken", script)
