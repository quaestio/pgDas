"""INSERT, UPDATE, TRUNCATE and DROP from the SQL runner: parsing, the preview, the confirmation words, the audit record."""
import json

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from audit.models import AuditEntry

from . import changes, pg
from .tests import admin_connect, TARGET
from .tests_writes import WriteFixture


class ClassifyAndParseTests(SimpleTestCase):
    def test_classify_finds_the_seven_changing_statements(self):
        cases = {"INSERT INTO t VALUES (1)": "insert", "  update t set a = 1": "update", "-- note\nDELETE FROM t": "delete",
                 "/* x */ Truncate t": "truncate", "drop table t;": "drop", "DROP": "drop",
                 "CREATE TABLE t (a int)": "create", "alter table t add c int": "alter",
                 "GRANT SELECT ON t TO u": "grant", "revoke all on t from u": "revoke"}
        for text, kind in cases.items():
            with self.subTest(text=text):
                self.assertEqual(changes.classify(text), kind)
        for text in ("SELECT 1", "WITH x AS (SELECT 1) DELETE FROM t", "EXPLAIN SELECT 1", "", "   ", "COPY t FROM stdin",
                     "COMMENT ON TABLE t IS 'x'", "VACUUM t"):
            with self.subTest(text=text), self.assertRaisesRegex(pg.PgError, "Only INSERT, UPDATE, DELETE, TRUNCATE, DROP, CREATE, ALTER, GRANT and REVOKE"):
                changes.classify(text)

    def test_parse_update(self):
        cases = {
            "UPDATE t SET a = 1 WHERE b = 2": ("t", "t", True),
            "update public.t set a = 1": ("public.t", "public.t", False),
            "UPDATE ONLY t AS x SET a = 1 WHERE x.b = 2": ("t", "x", True),
            "UPDATE t x SET a = 1 FROM u WHERE x.id = u.id": ("t", "x", True),
            'UPDATE "My T" SET "a b" = (SELECT max(n) FROM s) WHERE id IN (1, 2)': ('"My T"', '"My T"', True),
            "UPDATE t SET note = 'where; or ) not' WHERE id = 1;": ("t", "t", True),
        }
        for text, (table, target, has_where) in cases.items():
            with self.subTest(text=text):
                parsed = changes.parse_update(text)
                self.assertEqual((parsed["table_ref"], parsed["target"], parsed["has_where"]), (table, target, has_where))
        for text, message in {"": "Enter a UPDATE", "UPDATE t SET a = 1; UPDATE u SET b = 2": "one UPDATE statement",
                              "UPDATE t SET a = 1 RETURNING *": "RETURNING", "WITH x AS (SELECT 1) UPDATE t SET a = 1": "WITH clause",
                              "UPDATE t SET a = (1": "parentheses", "DELETE FROM t": "not a UPDATE", "UPDATE": "Write it as UPDATE"}.items():
            with self.subTest(text=text), self.assertRaisesRegex(pg.PgError, message):
                changes.parse_update(text)

    def test_parse_insert(self):
        self.assertEqual(changes.parse_insert("insert into public . t (a) values (1)")["table_ref"], "public.t")
        self.assertEqual(changes.parse_insert("INSERT INTO t (a) VALUES (1) RETURNING a")["table_ref"], "t")     # RETURNING is fine
        for text, message in {"": "Enter a INSERT", "INSERT INTO t VALUES (1); INSERT INTO t VALUES (2)": "one INSERT statement",
                              "INSERT t VALUES (1)": "Write it as INSERT INTO", "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x": "WITH clause",
                              "INSERT INTO t VALUES (1": "parentheses"}.items():
            with self.subTest(text=text), self.assertRaisesRegex(pg.PgError, message):
                changes.parse_insert(text)

    def test_parse_truncate(self):
        parsed = changes.parse_truncate("TRUNCATE TABLE ONLY a, public . b* RESTART IDENTITY CASCADE;")
        self.assertEqual((parsed["tables"], parsed["cascade"], parsed["restart_identity"]), (["a", "public.b"], True, True))
        parsed = changes.parse_truncate('truncate "My T"')
        self.assertEqual((parsed["tables"], parsed["cascade"], parsed["restart_identity"]), (['"My T"'], False, False))
        self.assertEqual(changes.parse_truncate("TRUNCATE a CONTINUE IDENTITY RESTRICT")["restart_identity"], False)
        for text in ("TRUNCATE", "TRUNCATE a b", "TRUNCATE a,", "TRUNCATE a; TRUNCATE b", "TRUNCATE (a)", "DROP TABLE a"):
            with self.subTest(text=text), self.assertRaises(pg.PgError):
                changes.parse_truncate(text)

    def test_parse_drop(self):
        parsed = changes.parse_drop("DROP TABLE IF EXISTS public.a, b CASCADE")
        self.assertEqual((parsed["object_type"], parsed["names"], parsed["cascade"]), ("TABLE", ["public.a", "b"], True))
        parsed = changes.parse_drop("drop materialized   view v restrict")
        self.assertEqual((parsed["object_type"], parsed["names"], parsed["cascade"]), ("MATERIALIZED VIEW", ["v"], False))
        parsed = changes.parse_drop("DROP FUNCTION f(int, text)")
        self.assertEqual((parsed["object_type"], parsed["names"]), ("FUNCTION", []))        # no row counts for these
        self.assertEqual(changes.parse_drop("DROP SCHEMA s CASCADE")["object_type"], "SCHEMA")
        self.assertEqual(changes.parse_drop("DROP EVENT TRIGGER e")["object_type"], "EVENT TRIGGER")
        for text in ("DROP DATABASE d", "DROP ROLE r", "drop user u", "DROP TABLESPACE t", "DROP OWNED BY r"):
            with self.subTest(text=text), self.assertRaisesRegex(pg.PgError, "not available here"):
                changes.parse_drop(text)
        for text in ("DROP", "DROP WIDGET w", "DROP TABLE a; DROP TABLE b", "SELECT 1"):
            with self.subTest(text=text), self.assertRaises(pg.PgError):
                changes.parse_drop(text)


class ChangeFixture(WriteFixture):
    def preview(self, sql):
        return changes.preview(self.server, self.db, sql)

    def execute(self, sql, fingerprint=None, confirm=None):
        return changes.execute(self.server, self.db, sql, fingerprint, confirm, self.staff)

    def exists(self, table):
        return bool(self.query("SELECT to_regclass(%s)", f"pgdas_w.{table}")[0][0])


class UpdateTests(ChangeFixture, TestCase):
    def test_preview_counts_the_rows_and_changes_nothing(self):
        data = self.preview("UPDATE pgdas_w.parent SET note = 'changed' WHERE id <= 3")
        self.assertEqual((data["kind"], data["count"], data["has_where"], data["confirm_word"]), ("update", 3, True, None))
        self.assertEqual((data["title"], data["headline"], data["button"]), ("UPDATE 3 RECORDS?", "3", "Update 3 records"))
        self.assertIn("will be changed in pgdas_w.parent", data["headline_label"])
        self.assertIn("no rollback", data["warning"])
        self.assertEqual([row["note"] for row in data["sample"]], ["changed"] * 3)           # rows as they WILL look
        self.assertEqual(data["columns"], ["id", "name", "data", "amount", "born", "seen", "blob", "tags", "note"])
        self.assertEqual(data["fingerprint"], {"count": 3, "side_effects": [], "undo": True})
        self.assertEqual(self.query("SELECT count(*) FROM pgdas_w.parent WHERE note = 'changed'")[0][0], 0)    # nothing changed
        self.assertFalse(AuditEntry.objects.exists())

    def test_no_where_clause_is_warned_about(self):
        data = self.preview("UPDATE pgdas_w.parent SET note = 'x'")
        self.assertEqual((data["count"], data["has_where"]), (10, False))
        warning = [n for n in data["notes"] if "no WHERE clause" in n["text"]]
        self.assertTrue(warning and warning[0]["bad"])
        self.assertIn("EVERY row", warning[0]["text"])
        self.assertFalse([n for n in self.preview("UPDATE pgdas_w.parent SET note = 'x' WHERE id = 1")["notes"] if "no WHERE" in n["text"]])
        self.assertEqual(len(data["sample"]), 5)
        self.assertTrue(any("first 5 of 10" in n["text"] for n in data["notes"]))

    def test_nothing_matching(self):
        data = self.preview("UPDATE pgdas_w.parent SET note = 'x' WHERE id = 999")
        self.assertEqual((data["count"], data["button"], data["title"]), (0, None, "UPDATE — NOTHING TO CHANGE"))
        with self.assertRaisesRegex(pg.PgError, "nothing to update"):
            self.execute("UPDATE pgdas_w.parent SET note = 'x' WHERE id = 999", {"count": 0, "side_effects": [], "undo": True})

    def test_execute_changes_the_rows_and_records_it(self):
        sql = "UPDATE pgdas_w.parent SET note = 'changed', amount = amount + 1 WHERE id IN (2, 4)"
        result = self.execute(sql, {"count": 2, "side_effects": [], "undo": True})          # no word is needed for UPDATE
        self.assertEqual((result["kind"], result["count"], result["table"]), ("update", 2, "parent"))
        self.assertIn("Updated 2 rows in pgdas_w.parent", result["message"])
        self.assertEqual([r[0] for r in self.query("SELECT id FROM pgdas_w.parent WHERE note = 'changed' ORDER BY id")], [2, 4])
        entry = AuditEntry.objects.get(pk=result["entry_id"])
        self.assertEqual((entry.action, entry.ok, entry.rows, entry.detail), ("update", True, 2, sql))
        payload = entry.payload
        self.assertEqual((payload["kind"], payload["schema"], payload["table"], payload["count"], payload["no_where"]),
                         ("update", "pgdas_w", "parent", 2, False))
        self.assertEqual(payload["database"], self.db)
        self.assertNotIn("rows", payload)                                       # an UPDATE's old values are not kept
        self.assertFalse(entry.can_reinsert)
        self.assertEqual(entry.record_label, "UPDATE · 2 rows in pgdas_w.parent")

    def test_an_update_with_no_where_is_recorded_as_such(self):
        result = self.execute("UPDATE pgdas_w.ident SET label = 'same'", {"count": 5, "side_effects": [], "undo": True})
        self.assertTrue(AuditEntry.objects.get(pk=result["entry_id"]).payload["no_where"])

    def test_the_data_must_still_be_what_was_shown(self):
        with self.assertRaisesRegex(pg.PgError, "changed since you checked"):
            self.execute("UPDATE pgdas_w.parent SET note = 'x' WHERE id <= 3", {"count": 2, "side_effects": [], "undo": True})
        self.assertEqual(self.query("SELECT count(*) FROM pgdas_w.parent WHERE note = 'x'")[0][0], 0)
        self.assertFalse(AuditEntry.objects.exists())

    def test_a_trigger_changing_another_table_is_reported(self):
        with admin_connect(self.db) as conn:
            conn.execute("""CREATE FUNCTION pgdas_w.note_update() RETURNS trigger LANGUAGE plpgsql AS
                            $$ BEGIN INSERT INTO pgdas_w.log (msg) VALUES ('updated ' || NEW.id); RETURN NEW; END $$""")
            conn.execute("CREATE TRIGGER logged_update AFTER UPDATE ON pgdas_w.logged FOR EACH ROW EXECUTE FUNCTION pgdas_w.note_update()")
        data = self.preview("UPDATE pgdas_w.logged SET what = 'z'")
        self.assertEqual(data["side_effects"], [{"table": "pgdas_w.log", "inserted": 2, "updated": 0, "deleted": 0}])
        self.assertTrue(any("also changes pgdas_w.log (2 inserted)" in n["text"] and n["bad"] for n in data["notes"]))
        with self.assertRaisesRegex(pg.PgError, "changed since you checked"):
            self.execute("UPDATE pgdas_w.logged SET what = 'z'", {"count": 2, "side_effects": [], "undo": True})     # the side effect was not accepted
        self.assertEqual(self.count("log"), 0)

    def test_what_cannot_be_updated(self):
        for sql, message in (("UPDATE pgdas_w.nope SET a = 1", "was not found"), ("UPDATE pgdas_w.parent_view SET name = 'x'", "ordinary tables"),
                             ("UPDATE pgdas_w.parent SET nocolumn = 1", "nocolumn"), ("UPDATE pgdas_w.parent SET id = 'text'", "integer"),
                             ("UPDATE pgdas_w.parent SET note = 'x' RETURNING *", "RETURNING")):
            with self.subTest(sql=sql), self.assertRaisesRegex(pg.PgError, message):
                self.preview(sql)

    def test_a_constraint_violation_is_explained_and_changes_nothing(self):
        with self.assertRaisesRegex(pg.PgError, "null value"):
            self.execute("UPDATE pgdas_w.parent SET name = NULL WHERE id = 1", {"count": 1, "side_effects": [], "undo": True})
        self.assertFalse(AuditEntry.objects.exists())


class UndoUpdateTests(ChangeFixture, TestCase):
    def update(self, sql, count):
        fingerprint = self.preview(sql)["fingerprint"]
        self.assertEqual(fingerprint["count"], count)
        return self.execute(sql, fingerprint)

    def note(self, row_id):
        return self.query("SELECT note FROM pgdas_w.parent WHERE id = %s", row_id)[0][0]

    def test_an_update_keeps_old_and_new_values_and_can_be_undone(self):
        preview = self.preview("UPDATE pgdas_w.parent SET note = 'changed' WHERE id IN (2, 4)")
        self.assertIn("undo the update", preview["warning"])
        self.assertTrue(preview["fingerprint"]["undo"])
        self.assertEqual(self.note(2), 'note 2')
        result = self.update("UPDATE pgdas_w.parent SET note = 'changed' WHERE id IN (2, 4)", 2)
        entry = AuditEntry.objects.get(pk=result["entry_id"])
        undo = entry.payload["undo"]
        self.assertEqual((undo["pk"], undo["columns"]), (["id"], ["note"]))
        self.assertEqual(sorted((r["id"], r["note"]) for r in undo["old"]), [(2, "note 2"), (4, "note 4")])
        self.assertEqual(sorted((r["id"], r["note"]) for r in undo["new"]), [(2, "changed"), (4, "changed")])
        self.assertTrue(entry.can_undo)
        self.assertEqual(self.note(2), "changed")
        done = changes.undo_update(entry, self.staff)
        self.assertEqual(done["count"], 2)
        self.assertEqual((self.note(2), self.note(4)), ("note 2", "note 4"))
        entry.refresh_from_db()
        self.assertEqual((entry.restored_by, entry.can_undo), (self.staff.username, False))
        log = AuditEntry.objects.get(action="update.undo")
        self.assertEqual((log.rows, log.payload["undone_entry"], log.record_label), (2, entry.pk, "undo · 2 rows in pgdas_w.parent"))
        with self.assertRaisesRegex(pg.PgError, "already undone"):
            changes.undo_update(entry, self.staff)

    def test_undo_is_refused_when_the_rows_were_changed_again(self):
        entry = AuditEntry.objects.get(pk=self.update("UPDATE pgdas_w.parent SET note = 'one' WHERE id <= 3", 3)["entry_id"])
        with admin_connect(self.db) as conn:
            conn.execute("UPDATE pgdas_w.parent SET note = 'newer' WHERE id = 2")
        with self.assertRaisesRegex(pg.PgError, "1 of the 3 rows were changed or deleted again"):
            changes.undo_update(entry, self.staff)
        self.assertEqual((self.note(1), self.note(2)), ("one", "newer"))             # nothing was touched
        entry.refresh_from_db()
        self.assertIsNone(entry.restored_at)
        with admin_connect(self.db) as conn:
            conn.execute("DELETE FROM pgdas_w.parent WHERE id = 3")
        with self.assertRaisesRegex(pg.PgError, "2 of the 3 rows"):
            changes.undo_update(entry, self.staff)

    def test_only_the_changed_columns_are_put_back_and_generated_ones_follow(self):
        entry = AuditEntry.objects.get(pk=self.update("UPDATE pgdas_w.ident SET label = 'much longer label' WHERE id <= 2", 2)["entry_id"])
        self.assertEqual(entry.payload["undo"]["columns"], ["label"])                # `doubled` is generated, so not set directly
        changes.undo_update(entry, self.staff)
        self.assertEqual(self.query("SELECT label, doubled FROM pgdas_w.ident WHERE id = 1")[0], ("label 1", 14))

    def test_without_a_primary_key_there_is_no_undo_but_the_update_works(self):
        with admin_connect(self.db) as conn:
            conn.execute("CREATE TABLE pgdas_w.loose (a int, b text)")
            conn.execute("ALTER TABLE pgdas_w.loose REPLICA IDENTITY FULL")
            conn.execute("INSERT INTO pgdas_w.loose VALUES (1, 'x'), (2, 'y')")
        sql = "UPDATE pgdas_w.loose SET b = 'z'"
        data = self.preview(sql)
        self.assertFalse(data["fingerprint"]["undo"])
        self.assertIn("The old values are not saved", data["warning"])
        self.assertTrue(any("no primary key" in n["text"] and n["bad"] for n in data["notes"]))
        entry = AuditEntry.objects.get(pk=self.execute(sql, data["fingerprint"])["entry_id"])
        self.assertIn("no primary key", entry.payload["undo"]["unavailable"])
        self.assertFalse(entry.can_undo)
        with self.assertRaisesRegex(pg.PgError, "cannot be undone: the table has no primary key"):
            changes.undo_update(entry, self.staff)

    def test_too_many_rows_or_a_changed_key_means_no_undo(self):
        with override_settings(MAX_UNDO_ROWS=3):
            data = self.preview("UPDATE pgdas_w.parent SET note = 'x'")
            self.assertEqual((data["count"], data["fingerprint"]["undo"]), (10, False))
            self.assertTrue(any("more than 3 rows" in n["text"] for n in data["notes"]))
            self.assertEqual(self.preview("UPDATE pgdas_w.parent SET note = 'x' WHERE id <= 3")["fingerprint"]["undo"], True)
        data = self.preview("UPDATE pgdas_w.logged SET id = id + 100")
        self.assertEqual((data["count"], data["fingerprint"]["undo"]), (2, False))
        self.assertTrue(any("primary key" in n["text"] for n in data["notes"]))
        data = self.preview("UPDATE pgdas_w.parent SET note = note WHERE id = 1")
        self.assertFalse(data["fingerprint"]["undo"])                                  # no value changes, nothing to put back

    def test_the_audit_page_has_an_undo_button_and_the_endpoint_works_once(self):
        entry = AuditEntry.objects.get(pk=self.update("UPDATE pgdas_w.parent SET note = 'one' WHERE id <= 2", 2)["entry_id"])
        self.client.force_login(self.staff)
        page = self.client.get(reverse("audit_list"))
        self.assertContains(page, reverse("audit_undo", args=[entry.pk]))
        self.assertContains(page, "Undo 2 rows")
        self.assertEqual(self.client.get(reverse("audit_undo", args=[entry.pk])).status_code, 405)
        response = self.client.post(reverse("audit_undo", args=[entry.pk]), {"page": "1"}, follow=True)
        self.assertContains(response, "Put back the old values of 2 rows in pgdas_w.parent")
        self.assertEqual(self.note(1), "note 1")
        self.assertNotContains(self.client.get(reverse("audit_list")), "Undo 2 rows")
        again = self.client.post(reverse("audit_undo", args=[entry.pk]), follow=True)
        self.assertContains(again, "already undone")
        self.assertTrue(AuditEntry.objects.filter(action="update.undo", ok=False).exists())
        plain = Client()
        plain.force_login(get_user_model().objects.create_user("pat", password="pw-12345-abc"))
        self.assertIn(plain.post(reverse("audit_undo", args=[entry.pk])).status_code, (302, 403))


class InsertTests(ChangeFixture, TestCase):
    def test_insert_runs_at_once_and_is_recorded(self):
        sql = "INSERT INTO pgdas_w.logged (id, what) VALUES (10, 'a'), (11, 'b'), (12, 'c')"
        result = self.execute(sql)                                              # no preview, no word, no popup
        self.assertEqual((result["kind"], result["count"], result["table"]), ("insert", 3, "logged"))
        self.assertIn("Inserted 3 rows into pgdas_w.logged", result["message"])
        self.assertEqual(self.count("logged"), 5)
        entry = AuditEntry.objects.get(pk=result["entry_id"])
        self.assertEqual((entry.action, entry.ok, entry.rows, entry.detail), ("insert", True, 3, sql))
        self.assertEqual((entry.payload["kind"], entry.payload["count"], entry.payload["database"]), ("insert", 3, self.db))
        self.assertEqual(entry.record_label, "INSERT · 3 rows into pgdas_w.logged")
        self.assertEqual(result["side_effects"], [{"table": "pgdas_w.log", "inserted": 0, "updated": 0, "deleted": 0}][:0])

    def test_insert_select_returning_and_conflicts(self):
        result = self.execute("INSERT INTO pgdas_w.logged SELECT id + 100, what FROM pgdas_w.logged")
        self.assertEqual(result["count"], 2)
        returning = self.execute("INSERT INTO pgdas_w.logged (id, what) VALUES (50, 'r') RETURNING id, what")
        self.assertEqual((returning["returned"]["columns"], returning["returned"]["rows"]), (["id", "what"], [["50", "r"]]))
        nothing = self.execute("INSERT INTO pgdas_w.logged (id, what) VALUES (1, 'dup') ON CONFLICT DO NOTHING")
        self.assertEqual(nothing["count"], 0)                                   # nothing inserted, and it says so
        self.assertEqual(self.count("logged"), 5)

    def test_a_failing_insert_changes_nothing_and_is_explained(self):
        for sql, message in (("INSERT INTO pgdas_w.logged (id, what) VALUES (1, 'dup')", "duplicate key"),
                             ("INSERT INTO pgdas_w.logged (id, what) VALUES (60, 'ok'), (1, 'dup')", "duplicate key"),
                             ("INSERT INTO pgdas_w.nope VALUES (1)", "was not found"),
                             ("INSERT INTO pgdas_w.parent_view VALUES (1, 'x')", "ordinary tables"),
                             ("INSERT INTO pgdas_w.logged (nocolumn) VALUES (1)", "nocolumn")):
            with self.subTest(sql=sql), self.assertRaisesRegex(pg.PgError, message):
                self.execute(sql)
        self.assertEqual(self.count("logged"), 2)                               # the half-good statement was rolled back too
        self.assertFalse(AuditEntry.objects.exists())

    def test_no_audit_record_means_no_insert(self):
        from unittest.mock import patch
        with patch("explorer.changes.record", side_effect=RuntimeError("audit store is full")):
            with self.assertRaisesRegex(pg.PgError, "could not save the audit record, so nothing was inserted"):
                self.execute("INSERT INTO pgdas_w.logged (id, what) VALUES (70, 'x')")
        self.assertEqual(self.count("logged"), 2)


class TruncateTests(ChangeFixture, TestCase):
    def test_preview_lists_the_tables_and_row_counts(self):
        data = self.preview("TRUNCATE pgdas_w.ident, pgdas_w.logged")
        self.assertEqual((data["kind"], data["count"], data["confirm_word"], data["title"], data["button"]),
                         ("truncate", 7, "TRUNCATE", "TRUNCATE 2 TABLES?", "Truncate 2 tables"))
        self.assertEqual(data["objects"], [{"name": "pgdas_w.ident", "rows": 5, "cascade": False}, {"name": "pgdas_w.logged", "rows": 2, "cascade": False}])
        self.assertEqual(data["headline"], "7")
        self.assertIn("2 tables at once", data["headline_label"])
        self.assertIn("cannot be rolled back", data["warning"])
        self.assertIn("NOT saved in the audit log", data["warning"])
        self.assertEqual((self.count("ident"), self.count("logged")), (5, 2))   # only a preview: nothing was emptied
        self.assertFalse(AuditEntry.objects.exists())

    def test_a_table_other_tables_point_at_needs_cascade_and_says_what_goes(self):
        with self.assertRaisesRegex(pg.PgError, "referenced in a foreign key constraint.*child.*CASCADE"):
            self.preview("TRUNCATE pgdas_w.parent")
        data = self.preview("TRUNCATE pgdas_w.parent CASCADE")
        self.assertEqual(data["objects"], [{"name": "pgdas_w.parent", "rows": 10, "cascade": False}, {"name": "pgdas_w.child", "rows": 20, "cascade": True}])
        self.assertEqual(data["count"], 30)
        also = [n for n in data["notes"] if "CASCADE also empties" in n["text"]]
        self.assertTrue(also and also[0]["bad"] and "pgdas_w.child (20 rows)" in also[0]["text"])
        self.assertEqual((self.count("parent"), self.count("child")), (10, 20))
        restart = self.preview("TRUNCATE pgdas_w.ident RESTART IDENTITY")
        self.assertTrue(any("RESTART IDENTITY" in n["text"] for n in restart["notes"]))

    def test_only_the_exact_word_truncate_confirms(self):
        fingerprint = self.preview("TRUNCATE pgdas_w.ident")["fingerprint"]
        for wrong in ("truncate", "Truncate", "TRUNCATE ", " TRUNCATE", "TRUNC", "ZAP", "DROP", "", None):
            with self.subTest(confirm=wrong), self.assertRaisesRegex(pg.PgError, "Type TRUNCATE in capital letters"):
                self.execute("TRUNCATE pgdas_w.ident", fingerprint, wrong)
        self.assertEqual(self.count("ident"), 5)
        self.assertFalse(AuditEntry.objects.exists())

    def test_confirmed_truncate_empties_the_tables_and_records_it(self):
        sql = "TRUNCATE pgdas_w.parent, pgdas_w.child"
        fingerprint = self.preview(sql)["fingerprint"]
        result = self.execute(sql, fingerprint, "TRUNCATE")
        self.assertEqual((result["kind"], result["count"]), ("truncate", 30))
        self.assertIn("Emptied 2 tables (30 rows)", result["message"])
        self.assertEqual((self.count("parent"), self.count("child")), (0, 0))
        entry = AuditEntry.objects.get(pk=result["entry_id"])
        self.assertEqual((entry.action, entry.ok, entry.rows), ("truncate", True, 30))
        payload = entry.payload
        self.assertEqual((payload["kind"], payload["total_rows"], payload["cascade"], payload["restart_identity"]), ("truncate", 30, False, False))
        self.assertEqual({t["name"]: t["rows"] for t in payload["tables"]}, {"pgdas_w.parent": 10, "pgdas_w.child": 20})
        self.assertNotIn("rows", {key for key in payload if isinstance(payload[key], list) and payload[key] and "id" in payload[key][0]})
        self.assertFalse(entry.can_reinsert)
        self.assertEqual(entry.record_label, "TRUNCATE · 2 tables, 30 rows")

    def test_cascade_and_restart_identity(self):
        fingerprint = self.preview("TRUNCATE pgdas_w.parent CASCADE")["fingerprint"]
        self.execute("TRUNCATE pgdas_w.parent CASCADE", fingerprint, "TRUNCATE")
        self.assertEqual((self.count("parent"), self.count("child")), (0, 0))
        fingerprint = self.preview("TRUNCATE pgdas_w.ident RESTART IDENTITY")["fingerprint"]
        result = self.execute("TRUNCATE pgdas_w.ident RESTART IDENTITY", fingerprint, "TRUNCATE")
        self.assertTrue(AuditEntry.objects.get(pk=result["entry_id"]).payload["restart_identity"])
        self.execute("INSERT INTO pgdas_w.ident (label) VALUES ('first again')")
        self.assertEqual(self.query("SELECT id FROM pgdas_w.ident")[0][0], 1)       # numbering started over

    def test_the_tables_must_still_be_what_was_shown(self):
        fingerprint = self.preview("TRUNCATE pgdas_w.logged")["fingerprint"]
        with admin_connect(self.db) as conn:
            conn.execute("INSERT INTO pgdas_w.logged VALUES (99, 'late arrival')")
        with self.assertRaisesRegex(pg.PgError, "changed since you checked"):
            self.execute("TRUNCATE pgdas_w.logged", fingerprint, "TRUNCATE")
        self.assertEqual(self.count("logged"), 3)                                # the late row was not silently destroyed
        self.assertFalse(AuditEntry.objects.exists())

    def test_what_cannot_be_truncated(self):
        for sql, message in (("TRUNCATE pgdas_w.nope", "was not found"), ("TRUNCATE pgdas_w.parent_view", "ordinary tables"),
                             ("TRUNCATE", "Write it as TRUNCATE")):
            with self.subTest(sql=sql), self.assertRaisesRegex(pg.PgError, message):
                self.preview(sql)


class DropTests(ChangeFixture, TestCase):
    def test_preview_of_a_table_shows_the_rows_that_would_be_lost(self):
        data = self.preview("DROP TABLE pgdas_w.logged")
        self.assertEqual((data["kind"], data["confirm_word"], data["title"], data["button"]), ("drop", "DROP", "DROP TABLE?", "Drop table"))
        self.assertEqual((data["headline"], data["objects"]), ("2", [{"name": "pgdas_w.logged", "rows": 2, "cascade": False}]))
        self.assertIn("rows will be lost", data["headline_label"])
        self.assertIn("no rollback", data["warning"])
        self.assertTrue(self.exists("logged"))                                   # only a preview: the table is still there
        self.assertFalse(AuditEntry.objects.exists())

    def test_what_depends_on_the_table_is_reported_or_blocks_the_drop(self):
        with self.assertRaisesRegex(pg.PgError, "other objects depend on it.*pgdas_w.parent_view.*CASCADE"):
            self.preview("DROP TABLE pgdas_w.parent")
        data = self.preview("DROP TABLE pgdas_w.parent CASCADE")
        cascades = [n for n in data["notes"] if "cascades" in n["text"]]
        self.assertTrue(cascades and all(n["bad"] for n in cascades))
        self.assertTrue(any("parent_view" in n["text"] for n in cascades))
        self.assertTrue(self.exists("parent") and self.exists("child"))

    def test_other_objects(self):
        view = self.preview("DROP VIEW pgdas_w.parent_view")
        self.assertEqual((view["title"], view["headline"], view["headline_label"], view["objects"]), ("DROP VIEW?", "VIEW", "will be permanently removed.", []))
        schema = self.preview("DROP SCHEMA pgdas_w CASCADE")
        self.assertEqual((schema["title"], schema["headline"]), ("DROP SCHEMA?", "SCHEMA"))
        self.assertTrue(any("cascades" in n["text"] for n in schema["notes"]))
        missing = self.preview("DROP TABLE IF EXISTS pgdas_w.not_there")
        self.assertTrue(any("does not exist, skipping" in n["text"] for n in missing["notes"]))
        self.assertTrue(self.exists("parent"))                                   # nothing above changed anything
        self.assertEqual(self.preview("DROP SEQUENCE pgdas_w.log_id_seq CASCADE")["title"], "DROP SEQUENCE?")

    def test_only_the_exact_word_drop_confirms(self):
        sql = "DROP TABLE pgdas_w.logged"
        fingerprint = self.preview(sql)["fingerprint"]
        for wrong in ("drop", "Drop", "DROP ", " DROP", "DROPP", "TRUNCATE", "ZAP", "", None):
            with self.subTest(confirm=wrong), self.assertRaisesRegex(pg.PgError, "Type DROP in capital letters"):
                self.execute(sql, fingerprint, wrong)
        self.assertTrue(self.exists("logged"))
        self.assertFalse(AuditEntry.objects.exists())

    def test_confirmed_drop_removes_the_object_and_records_it_with_its_definition(self):
        sql = "DROP TABLE pgdas_w.parent CASCADE"
        fingerprint = self.preview(sql)["fingerprint"]
        result = self.execute(sql, fingerprint, "DROP")
        self.assertEqual((result["kind"], result["count"]), ("drop", 10))
        self.assertFalse(self.exists("parent"))
        self.assertFalse(self.exists("parent_view"))                              # the dependent view went too, as announced
        self.assertTrue(self.exists("child"))                                     # only its foreign key went
        entry = AuditEntry.objects.get(pk=result["entry_id"])
        payload = entry.payload
        self.assertEqual((entry.action, entry.ok, entry.rows, payload["kind"], payload["object_type"], payload["cascade"]),
                         ("drop", True, 10, "drop", "TABLE", True))
        self.assertEqual(payload["tables"], [{"name": "pgdas_w.parent", "rows": 10}])
        self.assertTrue(any("parent_view" in notice for notice in payload["notices"]))
        self.assertTrue(payload["ddl"][0]["sql"].startswith("CREATE TABLE pgdas_w.parent ("))       # kept for reference, to recreate it
        self.assertFalse(entry.can_reinsert)
        self.assertEqual(entry.record_label, "DROP TABLE")
        self.assertIn("Dropped as asked (table)", result["message"])

    def test_dropping_a_schema_with_everything_in_it(self):
        with admin_connect(self.db) as conn:
            conn.execute("CREATE SCHEMA pgdas_gone")
            conn.execute("CREATE TABLE pgdas_gone.t (a int)")
            conn.execute("CREATE VIEW pgdas_gone.v AS SELECT a FROM pgdas_gone.t")
        fingerprint = self.preview("DROP SCHEMA pgdas_gone CASCADE")["fingerprint"]
        self.assertTrue(fingerprint["notices"])
        result = self.execute("DROP SCHEMA pgdas_gone CASCADE", fingerprint, "DROP")
        self.assertEqual(self.query("SELECT count(*) FROM pg_namespace WHERE nspname = 'pgdas_gone'")[0][0], 0)
        self.assertEqual(AuditEntry.objects.get(pk=result["entry_id"]).payload["object_type"], "SCHEMA")

    def test_what_is_dropped_must_still_be_what_was_shown(self):
        sql = "DROP TABLE pgdas_w.logged"
        fingerprint = self.preview(sql)["fingerprint"]
        with admin_connect(self.db) as conn:
            conn.execute("INSERT INTO pgdas_w.logged VALUES (77, 'new data')")
        with self.assertRaisesRegex(pg.PgError, "changed since you checked"):
            self.execute(sql, fingerprint, "DROP")
        self.assertTrue(self.exists("logged"))
        self.assertEqual(self.count("logged"), 3)
        self.assertFalse(AuditEntry.objects.exists())

    def test_what_is_refused(self):
        for sql, message in (("DROP TABLESPACE x", "not available here"), ("DROP OWNED BY x", "not available here"),
                             ("DROP WIDGET w", "PGDAS can drop tables"), ("DROP TABLE pgdas_w.a; DROP TABLE pgdas_w.b", "one DROP statement"),
                             ("DROP TABLE pgdas_w.nope", "does not exist")):
            with self.subTest(sql=sql), self.assertRaisesRegex(pg.PgError, message):
                self.preview(sql)


class CreateAlterTests(ChangeFixture, TestCase):
    def test_create_preview_lists_what_would_be_created_and_changes_nothing(self):
        data = self.preview("CREATE TABLE pgdas_w.made (id serial PRIMARY KEY, name text)")
        self.assertEqual((data["kind"], data["title"], data["button"], data["confirm_word"]), ("create", "CREATE TABLE?", "Create table", None))
        texts = [o["text"] for o in data["objects"]]
        self.assertTrue(any(t.startswith("+ create table pgdas_w.made") for t in texts))
        self.assertTrue(any("sequence pgdas_w.made_id_seq" in t for t in texts))
        self.assertTrue(any("index pgdas_w.made_pkey" in t for t in texts))
        self.assertFalse(self.exists("made"))
        self.assertFalse(AuditEntry.objects.exists())

    def test_create_executes_and_is_recorded(self):
        sql = "CREATE TABLE pgdas_w.made (id int, name text)"
        fingerprint = self.preview(sql)["fingerprint"]
        result = self.execute(sql, fingerprint)
        self.assertTrue(self.exists("made"))
        entry = AuditEntry.objects.get(pk=result["entry_id"])
        self.assertEqual((entry.action, entry.ok, entry.payload["kind"], entry.payload["object_type"]), ("create", True, "create", "TABLE"))
        self.assertEqual(entry.payload["changes"][0]["name"], "pgdas_w.made")
        self.assertTrue(entry.record_label.startswith("CREATE TABLE"))

    def test_create_that_does_nothing_has_no_button_and_will_not_run(self):
        data = self.preview("CREATE TABLE IF NOT EXISTS pgdas_w.logged (id int)")
        self.assertIsNone(data["button"])
        self.assertIn("NOTHING TO DO", data["title"])
        with self.assertRaisesRegex(pg.PgError, "nothing to run"):
            self.execute("CREATE TABLE IF NOT EXISTS pgdas_w.logged (id int)", data["fingerprint"])

    def test_create_of_something_that_exists_is_an_error(self):
        with self.assertRaisesRegex(pg.PgError, "already exists"):
            self.preview("CREATE TABLE pgdas_w.logged (id int)")

    def test_plain_alter_needs_no_word_and_lists_the_column(self):
        data = self.preview("ALTER TABLE pgdas_w.logged ADD COLUMN extra int")
        self.assertEqual((data["kind"], data["confirm_word"], data["risky"], data["button"]), ("alter", None, False, "Alter table"))
        self.assertTrue(any("columns: +extra" in o["text"] for o in data["objects"]))
        self.assertFalse(self.query("SELECT 1 FROM information_schema.columns WHERE table_schema='pgdas_w' AND table_name='logged' AND column_name='extra'"))
        result = self.execute("ALTER TABLE pgdas_w.logged ADD COLUMN extra int", data["fingerprint"])
        self.assertTrue(self.query("SELECT 1 FROM information_schema.columns WHERE table_schema='pgdas_w' AND table_name='logged' AND column_name='extra'"))
        payload = AuditEntry.objects.get(pk=result["entry_id"]).payload
        self.assertFalse(payload["risky"])
        self.assertTrue(payload["before"][0]["sql"].startswith("CREATE TABLE pgdas_w.logged"))   # the previous definition is kept

    def test_risky_alter_needs_the_exact_word(self):
        with admin_connect(self.db) as conn:
            conn.execute("ALTER TABLE pgdas_w.logged ADD COLUMN extra int")
        sql = "ALTER TABLE pgdas_w.logged DROP COLUMN extra"
        data = self.preview(sql)
        self.assertEqual((data["confirm_word"], data["risky"]), ("ALTER", True))
        for wrong in ("alter", "Alter", "ALTER ", "", None, "DROP"):
            with self.subTest(confirm=wrong), self.assertRaisesRegex(pg.PgError, "Type ALTER in capital letters"):
                self.execute(sql, data["fingerprint"], wrong)
        self.assertTrue(self.query("SELECT 1 FROM information_schema.columns WHERE table_schema='pgdas_w' AND table_name='logged' AND column_name='extra'"))
        self.assertFalse(AuditEntry.objects.exists())
        self.execute(sql, data["fingerprint"], "ALTER")
        self.assertFalse(self.query("SELECT 1 FROM information_schema.columns WHERE table_schema='pgdas_w' AND table_name='logged' AND column_name='extra'"))

    def test_rename_and_type_change_are_risky(self):
        for sql in ("ALTER TABLE pgdas_w.logged RENAME TO logged2", "ALTER TABLE pgdas_w.logged ALTER COLUMN id TYPE bigint"):
            with self.subTest(sql=sql):
                self.assertTrue(self.preview(sql)["risky"])

    def test_what_changed_must_still_be_what_was_shown(self):
        sql = "ALTER TABLE pgdas_w.logged ADD COLUMN extra int"
        fingerprint = self.preview(sql)["fingerprint"]
        fingerprint["changes"][0]["detail"] = "columns: +something_else"          # not what the statement would now do
        with self.assertRaisesRegex(pg.PgError, "changed since you checked"):
            self.execute(sql, fingerprint)
        with self.assertRaisesRegex(pg.PgError, "changed since you checked"):
            self.execute(sql, {})
        self.assertFalse(AuditEntry.objects.exists())

    def test_what_is_refused(self):
        for sql, message in (("CREATE TABLESPACE x LOCATION '/tmp'", "not available here"), ("CREATE SERVER s FOREIGN DATA WRAPPER w", "not available here"),
                             ("CREATE TEMP TABLE x (a int)", "Temporary objects"), ("CREATE INDEX CONCURRENTLY i ON pgdas_w.logged (id)", "CONCURRENTLY"),
                             ("CREATE TABLE pgdas_w.a (a int); CREATE TABLE pgdas_w.b (a int)", "one CREATE statement"),
                             ("ALTER SYSTEM SET work_mem = '8MB'", "not available here"), ("ALTER TABLESPACE x RENAME TO y", "not available here")):
            with self.subTest(sql=sql), self.assertRaisesRegex(pg.PgError, message):
                self.preview(sql)


class ChangeEndpointTests(ChangeFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)

    def post(self, name, data, client=None):
        return (client or self.client).post(reverse(name, args=[self.server.pk]), json.dumps(data), content_type="application/json")

    def test_preview_endpoint_for_every_kind_that_has_one(self):
        for sql, kind, word in (("UPDATE pgdas_w.parent SET note = 'x' WHERE id < 3", "update", None),
                                ("DELETE FROM pgdas_w.parent WHERE id < 3", "delete", "ZAP"),
                                ("TRUNCATE pgdas_w.ident", "truncate", "TRUNCATE"), ("DROP TABLE pgdas_w.logged", "drop", "DROP")):
            with self.subTest(kind=kind):
                body = self.post("write_preview", {"sql": sql, "db": self.db}).json()
                self.assertEqual((body["ok"], body["kind"], body["confirm_word"]), (True, kind, word))
                for key in ("title", "headline", "headline_label", "warning", "notes", "fingerprint", "statement"):
                    self.assertIn(key, body)
        insert = self.post("write_preview", {"sql": "INSERT INTO pgdas_w.logged VALUES (5, 'x')", "db": self.db})
        self.assertEqual(insert.status_code, 400)
        self.assertIn("nothing to preview", insert.json()["error"])
        self.assertEqual(self.count("parent"), 10)

    def test_insert_and_update_execute_without_a_word(self):
        insert = self.post("write_execute", {"sql": "INSERT INTO pgdas_w.logged (id, what) VALUES (5, 'five')", "db": self.db})
        self.assertEqual((insert.status_code, insert.json()["ok"], insert.json()["count"]), (200, True, 1))
        preview = self.post("write_preview", {"sql": "UPDATE pgdas_w.logged SET what = 'six' WHERE id = 5", "db": self.db}).json()
        update = self.post("write_execute", {"sql": "UPDATE pgdas_w.logged SET what = 'six' WHERE id = 5", "db": self.db,
                                             "fingerprint": preview["fingerprint"]})
        self.assertEqual((update.status_code, update.json()["ok"], update.json()["count"]), (200, True, 1))
        self.assertEqual(self.query("SELECT what FROM pgdas_w.logged WHERE id = 5")[0][0], "six")
        self.assertEqual([e.action for e in AuditEntry.objects.order_by("pk")], ["insert", "update"])

    def test_only_staff_can_use_it(self):
        sql = "UPDATE pgdas_w.parent SET note = 'x'"
        plain = Client()
        plain.force_login(get_user_model().objects.create_user("pat", password="pw-12345-abc"))
        for name in ("write_preview", "write_execute"):
            response = self.post(name, {"sql": sql, "db": self.db}, client=plain)
            self.assertEqual(response.status_code, 403)
            self.assertIn("Only staff", response.json()["error"])
        self.assertEqual(self.query("SELECT count(*) FROM pgdas_w.parent WHERE note = 'x'")[0][0], 0)

    def test_a_refused_attempt_is_logged_under_its_own_kind(self):
        response = self.post("write_execute", {"sql": "TRUNCATE pgdas_w.ident", "db": self.db, "confirm": "truncate",
                                               "fingerprint": {"tables": []}})
        self.assertEqual(response.status_code, 400)
        entry = AuditEntry.objects.get()
        self.assertEqual((entry.action, entry.ok), ("truncate", False))
        self.assertEqual(self.count("ident"), 5)

    def test_audit_log_page_shows_every_kind_of_record(self):
        self.execute("INSERT INTO pgdas_w.logged (id, what) VALUES (30, 'x')")
        self.execute("UPDATE pgdas_w.logged SET what = 'y' WHERE id = 30", {"count": 1, "side_effects": [], "undo": True})
        fingerprint = self.preview("TRUNCATE pgdas_w.ident")["fingerprint"]
        self.execute("TRUNCATE pgdas_w.ident", fingerprint, "TRUNCATE")
        fingerprint = self.preview("DROP VIEW pgdas_w.parent_view")["fingerprint"]
        self.execute("DROP VIEW pgdas_w.parent_view", fingerprint, "DROP")
        page = self.client.get(reverse("audit_list"))
        for text in ("INSERT · 1 row into pgdas_w.logged", "UPDATE · 1 row in pgdas_w.logged", "TRUNCATE · 1 table, 5 rows",
                     "DROP VIEW", "&quot;kind&quot;: &quot;truncate&quot;", "cannot be re-inserted from the audit log"):
            self.assertContains(page, text)
        self.assertNotContains(page, "Re-insert ")                                # only deletes can be put back

    def test_the_server_form_has_no_change_switches(self):
        page = self.client.get(reverse("server_edit", args=[self.server.pk]))
        for text in ("Allow changing data", "Also allow", "allow_writes", "allow_structure", "allow_admin"):
            self.assertNotContains(page, text)

    def test_popup_markup_and_script_cover_all_statements(self):
        page = self.client.get(reverse("sql_runner", args=[self.server.pk]))
        for text in ('id="changeWarning"', 'id="changeObjects"', 'id="changeWord"', "INSERT</strong> runs at once", "UPDATE</strong> shows how many rows",
                     "type ZAP", "type TRUNCATE or DROP", "CREATE</strong> and <strong>ALTER"):
            self.assertContains(page, text)
        script = open("static/review.js", encoding="utf-8").read() + open("static/write_flow.js", encoding="utf-8").read()
        for needle in ('kind === "insert"', "preview.confirm_word", "preview.fingerprint", "preview.objects", "preview.notes",
                       "(insert|update|delete|truncate|drop|create|alter|grant|revoke)", "tone(\"error\")", "tone(\"success\")"):
            self.assertIn(needle, script)
