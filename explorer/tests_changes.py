"""INSERT, UPDATE, TRUNCATE and DROP from the SQL runner: parsing, the preview, the confirmation words, the audit record."""
import json

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from audit.models import AuditEntry

from . import changes, pg
from .tests import admin_connect, TARGET
from .tests_writes import WriteFixture


class ClassifyAndParseTests(SimpleTestCase):
    def test_classify_finds_the_five_changing_statements(self):
        cases = {"INSERT INTO t VALUES (1)": "insert", "  update t set a = 1": "update", "-- note\nDELETE FROM t": "delete",
                 "/* x */ Truncate t": "truncate", "drop table t;": "drop", "DROP": "drop"}
        for text, kind in cases.items():
            with self.subTest(text=text):
                self.assertEqual(changes.classify(text), kind)
        for text in ("SELECT 1", "WITH x AS (SELECT 1) DELETE FROM t", "ALTER TABLE t ADD c int", "CREATE TABLE t (a int)",
                     "EXPLAIN SELECT 1", "", "   ", "COPY t FROM stdin", "GRANT ALL ON t TO u"):
            with self.subTest(text=text), self.assertRaisesRegex(pg.PgError, "Only INSERT, UPDATE, DELETE, TRUNCATE and DROP"):
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
    def setUp(self):
        super().setUp()
        self.server.allow_structure = True
        self.server.save()

    def preview(self, sql):
        return changes.preview(self.server, self.db, sql)

    def execute(self, sql, fingerprint=None, confirm=None):
        return changes.execute(self.server, self.db, sql, fingerprint, confirm, self.staff)

    def exists(self, table):
        return bool(self.query("SELECT to_regclass(%s)", f"pgdeck_w.{table}")[0][0])


class UpdateTests(ChangeFixture, TestCase):
    def test_preview_counts_the_rows_and_changes_nothing(self):
        data = self.preview("UPDATE pgdeck_w.parent SET note = 'changed' WHERE id <= 3")
        self.assertEqual((data["kind"], data["count"], data["has_where"], data["confirm_word"]), ("update", 3, True, None))
        self.assertEqual((data["title"], data["headline"], data["button"]), ("UPDATE 3 RECORDS?", "3", "Update 3 records"))
        self.assertIn("will be changed in pgdeck_w.parent", data["headline_label"])
        self.assertIn("no rollback", data["warning"])
        self.assertEqual([row["note"] for row in data["sample"]], ["changed"] * 3)           # rows as they WILL look
        self.assertEqual(data["columns"], ["id", "name", "data", "amount", "born", "seen", "blob", "tags", "note"])
        self.assertEqual(data["fingerprint"], {"count": 3, "side_effects": []})
        self.assertEqual(self.query("SELECT count(*) FROM pgdeck_w.parent WHERE note = 'changed'")[0][0], 0)    # nothing changed
        self.assertFalse(AuditEntry.objects.exists())

    def test_no_where_clause_is_warned_about(self):
        data = self.preview("UPDATE pgdeck_w.parent SET note = 'x'")
        self.assertEqual((data["count"], data["has_where"]), (10, False))
        warning = [n for n in data["notes"] if "no WHERE clause" in n["text"]]
        self.assertTrue(warning and warning[0]["bad"])
        self.assertIn("EVERY row", warning[0]["text"])
        self.assertFalse([n for n in self.preview("UPDATE pgdeck_w.parent SET note = 'x' WHERE id = 1")["notes"] if "no WHERE" in n["text"]])
        self.assertEqual(len(data["sample"]), 5)
        self.assertTrue(any("first 5 of 10" in n["text"] for n in data["notes"]))

    def test_nothing_matching(self):
        data = self.preview("UPDATE pgdeck_w.parent SET note = 'x' WHERE id = 999")
        self.assertEqual((data["count"], data["button"], data["title"]), (0, None, "UPDATE — NOTHING TO CHANGE"))
        with self.assertRaisesRegex(pg.PgError, "nothing to update"):
            self.execute("UPDATE pgdeck_w.parent SET note = 'x' WHERE id = 999", {"count": 0, "side_effects": []})

    def test_execute_changes_the_rows_and_records_it(self):
        sql = "UPDATE pgdeck_w.parent SET note = 'changed', amount = amount + 1 WHERE id IN (2, 4)"
        result = self.execute(sql, {"count": 2, "side_effects": []})          # no word is needed for UPDATE
        self.assertEqual((result["kind"], result["count"], result["table"]), ("update", 2, "parent"))
        self.assertIn("Updated 2 rows in pgdeck_w.parent", result["message"])
        self.assertEqual([r[0] for r in self.query("SELECT id FROM pgdeck_w.parent WHERE note = 'changed' ORDER BY id")], [2, 4])
        entry = AuditEntry.objects.get(pk=result["entry_id"])
        self.assertEqual((entry.action, entry.ok, entry.rows, entry.detail), ("update", True, 2, sql))
        payload = entry.payload
        self.assertEqual((payload["kind"], payload["schema"], payload["table"], payload["count"], payload["no_where"]),
                         ("update", "pgdeck_w", "parent", 2, False))
        self.assertEqual(payload["database"], self.db)
        self.assertNotIn("rows", payload)                                       # an UPDATE's old values are not kept
        self.assertFalse(entry.can_reinsert)
        self.assertEqual(entry.record_label, "UPDATE · 2 rows in pgdeck_w.parent")

    def test_an_update_with_no_where_is_recorded_as_such(self):
        result = self.execute("UPDATE pgdeck_w.ident SET label = 'same'", {"count": 5, "side_effects": []})
        self.assertTrue(AuditEntry.objects.get(pk=result["entry_id"]).payload["no_where"])

    def test_the_data_must_still_be_what_was_shown(self):
        with self.assertRaisesRegex(pg.PgError, "changed since you checked"):
            self.execute("UPDATE pgdeck_w.parent SET note = 'x' WHERE id <= 3", {"count": 2, "side_effects": []})
        self.assertEqual(self.query("SELECT count(*) FROM pgdeck_w.parent WHERE note = 'x'")[0][0], 0)
        self.assertFalse(AuditEntry.objects.exists())

    def test_a_trigger_changing_another_table_is_reported(self):
        with admin_connect(self.db) as conn:
            conn.execute("""CREATE FUNCTION pgdeck_w.note_update() RETURNS trigger LANGUAGE plpgsql AS
                            $$ BEGIN INSERT INTO pgdeck_w.log (msg) VALUES ('updated ' || NEW.id); RETURN NEW; END $$""")
            conn.execute("CREATE TRIGGER logged_update AFTER UPDATE ON pgdeck_w.logged FOR EACH ROW EXECUTE FUNCTION pgdeck_w.note_update()")
        data = self.preview("UPDATE pgdeck_w.logged SET what = 'z'")
        self.assertEqual(data["side_effects"], [{"table": "pgdeck_w.log", "inserted": 2, "updated": 0, "deleted": 0}])
        self.assertTrue(any("also changes pgdeck_w.log (2 inserted)" in n["text"] and n["bad"] for n in data["notes"]))
        with self.assertRaisesRegex(pg.PgError, "changed since you checked"):
            self.execute("UPDATE pgdeck_w.logged SET what = 'z'", {"count": 2, "side_effects": []})     # the side effect was not accepted
        self.assertEqual(self.count("log"), 0)

    def test_what_cannot_be_updated(self):
        for sql, message in (("UPDATE pgdeck_w.nope SET a = 1", "was not found"), ("UPDATE pgdeck_w.parent_view SET name = 'x'", "ordinary tables"),
                             ("UPDATE pgdeck_w.parent SET nocolumn = 1", "nocolumn"), ("UPDATE pgdeck_w.parent SET id = 'text'", "integer"),
                             ("UPDATE pgdeck_w.parent SET note = 'x' RETURNING *", "RETURNING")):
            with self.subTest(sql=sql), self.assertRaisesRegex(pg.PgError, message):
                self.preview(sql)

    def test_a_constraint_violation_is_explained_and_changes_nothing(self):
        with self.assertRaisesRegex(pg.PgError, "null value"):
            self.execute("UPDATE pgdeck_w.parent SET name = NULL WHERE id = 1", {"count": 1, "side_effects": []})
        self.assertFalse(AuditEntry.objects.exists())


class InsertTests(ChangeFixture, TestCase):
    def test_insert_runs_at_once_and_is_recorded(self):
        sql = "INSERT INTO pgdeck_w.logged (id, what) VALUES (10, 'a'), (11, 'b'), (12, 'c')"
        result = self.execute(sql)                                              # no preview, no word, no popup
        self.assertEqual((result["kind"], result["count"], result["table"]), ("insert", 3, "logged"))
        self.assertIn("Inserted 3 rows into pgdeck_w.logged", result["message"])
        self.assertEqual(self.count("logged"), 5)
        entry = AuditEntry.objects.get(pk=result["entry_id"])
        self.assertEqual((entry.action, entry.ok, entry.rows, entry.detail), ("insert", True, 3, sql))
        self.assertEqual((entry.payload["kind"], entry.payload["count"], entry.payload["database"]), ("insert", 3, self.db))
        self.assertEqual(entry.record_label, "INSERT · 3 rows into pgdeck_w.logged")
        self.assertEqual(result["side_effects"], [{"table": "pgdeck_w.log", "inserted": 0, "updated": 0, "deleted": 0}][:0])

    def test_insert_select_returning_and_conflicts(self):
        result = self.execute("INSERT INTO pgdeck_w.logged SELECT id + 100, what FROM pgdeck_w.logged")
        self.assertEqual(result["count"], 2)
        returning = self.execute("INSERT INTO pgdeck_w.logged (id, what) VALUES (50, 'r') RETURNING id, what")
        self.assertEqual((returning["returned"]["columns"], returning["returned"]["rows"]), (["id", "what"], [["50", "r"]]))
        nothing = self.execute("INSERT INTO pgdeck_w.logged (id, what) VALUES (1, 'dup') ON CONFLICT DO NOTHING")
        self.assertEqual(nothing["count"], 0)                                   # nothing inserted, and it says so
        self.assertEqual(self.count("logged"), 5)

    def test_a_failing_insert_changes_nothing_and_is_explained(self):
        for sql, message in (("INSERT INTO pgdeck_w.logged (id, what) VALUES (1, 'dup')", "duplicate key"),
                             ("INSERT INTO pgdeck_w.logged (id, what) VALUES (60, 'ok'), (1, 'dup')", "duplicate key"),
                             ("INSERT INTO pgdeck_w.nope VALUES (1)", "was not found"),
                             ("INSERT INTO pgdeck_w.parent_view VALUES (1, 'x')", "ordinary tables"),
                             ("INSERT INTO pgdeck_w.logged (nocolumn) VALUES (1)", "nocolumn")):
            with self.subTest(sql=sql), self.assertRaisesRegex(pg.PgError, message):
                self.execute(sql)
        self.assertEqual(self.count("logged"), 2)                               # the half-good statement was rolled back too
        self.assertFalse(AuditEntry.objects.exists())

    def test_no_audit_record_means_no_insert(self):
        from unittest.mock import patch
        with patch("explorer.changes.record", side_effect=RuntimeError("audit store is full")):
            with self.assertRaisesRegex(pg.PgError, "could not save the audit record, so nothing was inserted"):
                self.execute("INSERT INTO pgdeck_w.logged (id, what) VALUES (70, 'x')")
        self.assertEqual(self.count("logged"), 2)


class TruncateTests(ChangeFixture, TestCase):
    def test_preview_lists_the_tables_and_row_counts(self):
        data = self.preview("TRUNCATE pgdeck_w.ident, pgdeck_w.logged")
        self.assertEqual((data["kind"], data["count"], data["confirm_word"], data["title"], data["button"]),
                         ("truncate", 7, "TRUNCATE", "TRUNCATE 2 TABLES?", "Truncate 2 tables"))
        self.assertEqual(data["objects"], [{"name": "pgdeck_w.ident", "rows": 5, "cascade": False}, {"name": "pgdeck_w.logged", "rows": 2, "cascade": False}])
        self.assertEqual(data["headline"], "7")
        self.assertIn("2 tables at once", data["headline_label"])
        self.assertIn("cannot be rolled back", data["warning"])
        self.assertIn("NOT saved in the audit log", data["warning"])
        self.assertEqual((self.count("ident"), self.count("logged")), (5, 2))   # only a preview: nothing was emptied
        self.assertFalse(AuditEntry.objects.exists())

    def test_a_table_other_tables_point_at_needs_cascade_and_says_what_goes(self):
        with self.assertRaisesRegex(pg.PgError, "referenced in a foreign key constraint.*child.*CASCADE"):
            self.preview("TRUNCATE pgdeck_w.parent")
        data = self.preview("TRUNCATE pgdeck_w.parent CASCADE")
        self.assertEqual(data["objects"], [{"name": "pgdeck_w.parent", "rows": 10, "cascade": False}, {"name": "pgdeck_w.child", "rows": 20, "cascade": True}])
        self.assertEqual(data["count"], 30)
        also = [n for n in data["notes"] if "CASCADE also empties" in n["text"]]
        self.assertTrue(also and also[0]["bad"] and "pgdeck_w.child (20 rows)" in also[0]["text"])
        self.assertEqual((self.count("parent"), self.count("child")), (10, 20))
        restart = self.preview("TRUNCATE pgdeck_w.ident RESTART IDENTITY")
        self.assertTrue(any("RESTART IDENTITY" in n["text"] for n in restart["notes"]))

    def test_only_the_exact_word_truncate_confirms(self):
        fingerprint = self.preview("TRUNCATE pgdeck_w.ident")["fingerprint"]
        for wrong in ("truncate", "Truncate", "TRUNCATE ", " TRUNCATE", "TRUNC", "ZAP", "DROP", "", None):
            with self.subTest(confirm=wrong), self.assertRaisesRegex(pg.PgError, "Type TRUNCATE in capital letters"):
                self.execute("TRUNCATE pgdeck_w.ident", fingerprint, wrong)
        self.assertEqual(self.count("ident"), 5)
        self.assertFalse(AuditEntry.objects.exists())

    def test_confirmed_truncate_empties_the_tables_and_records_it(self):
        sql = "TRUNCATE pgdeck_w.parent, pgdeck_w.child"
        fingerprint = self.preview(sql)["fingerprint"]
        result = self.execute(sql, fingerprint, "TRUNCATE")
        self.assertEqual((result["kind"], result["count"]), ("truncate", 30))
        self.assertIn("Emptied 2 tables (30 rows)", result["message"])
        self.assertEqual((self.count("parent"), self.count("child")), (0, 0))
        entry = AuditEntry.objects.get(pk=result["entry_id"])
        self.assertEqual((entry.action, entry.ok, entry.rows), ("truncate", True, 30))
        payload = entry.payload
        self.assertEqual((payload["kind"], payload["total_rows"], payload["cascade"], payload["restart_identity"]), ("truncate", 30, False, False))
        self.assertEqual({t["name"]: t["rows"] for t in payload["tables"]}, {"pgdeck_w.parent": 10, "pgdeck_w.child": 20})
        self.assertNotIn("rows", {key for key in payload if isinstance(payload[key], list) and payload[key] and "id" in payload[key][0]})
        self.assertFalse(entry.can_reinsert)
        self.assertEqual(entry.record_label, "TRUNCATE · 2 tables, 30 rows")

    def test_cascade_and_restart_identity(self):
        fingerprint = self.preview("TRUNCATE pgdeck_w.parent CASCADE")["fingerprint"]
        self.execute("TRUNCATE pgdeck_w.parent CASCADE", fingerprint, "TRUNCATE")
        self.assertEqual((self.count("parent"), self.count("child")), (0, 0))
        fingerprint = self.preview("TRUNCATE pgdeck_w.ident RESTART IDENTITY")["fingerprint"]
        result = self.execute("TRUNCATE pgdeck_w.ident RESTART IDENTITY", fingerprint, "TRUNCATE")
        self.assertTrue(AuditEntry.objects.get(pk=result["entry_id"]).payload["restart_identity"])
        self.execute("INSERT INTO pgdeck_w.ident (label) VALUES ('first again')")
        self.assertEqual(self.query("SELECT id FROM pgdeck_w.ident")[0][0], 1)       # numbering started over

    def test_the_tables_must_still_be_what_was_shown(self):
        fingerprint = self.preview("TRUNCATE pgdeck_w.logged")["fingerprint"]
        with admin_connect(self.db) as conn:
            conn.execute("INSERT INTO pgdeck_w.logged VALUES (99, 'late arrival')")
        with self.assertRaisesRegex(pg.PgError, "changed since you checked"):
            self.execute("TRUNCATE pgdeck_w.logged", fingerprint, "TRUNCATE")
        self.assertEqual(self.count("logged"), 3)                                # the late row was not silently destroyed
        self.assertFalse(AuditEntry.objects.exists())

    def test_what_cannot_be_truncated(self):
        for sql, message in (("TRUNCATE pgdeck_w.nope", "was not found"), ("TRUNCATE pgdeck_w.parent_view", "ordinary tables"),
                             ("TRUNCATE", "Write it as TRUNCATE")):
            with self.subTest(sql=sql), self.assertRaisesRegex(pg.PgError, message):
                self.preview(sql)


class DropTests(ChangeFixture, TestCase):
    def test_preview_of_a_table_shows_the_rows_that_would_be_lost(self):
        data = self.preview("DROP TABLE pgdeck_w.logged")
        self.assertEqual((data["kind"], data["confirm_word"], data["title"], data["button"]), ("drop", "DROP", "DROP TABLE?", "Drop table"))
        self.assertEqual((data["headline"], data["objects"]), ("2", [{"name": "pgdeck_w.logged", "rows": 2, "cascade": False}]))
        self.assertIn("rows will be lost", data["headline_label"])
        self.assertIn("no rollback", data["warning"])
        self.assertTrue(self.exists("logged"))                                   # only a preview: the table is still there
        self.assertFalse(AuditEntry.objects.exists())

    def test_what_depends_on_the_table_is_reported_or_blocks_the_drop(self):
        with self.assertRaisesRegex(pg.PgError, "other objects depend on it.*pgdeck_w.parent_view.*CASCADE"):
            self.preview("DROP TABLE pgdeck_w.parent")
        data = self.preview("DROP TABLE pgdeck_w.parent CASCADE")
        cascades = [n for n in data["notes"] if "cascades" in n["text"]]
        self.assertTrue(cascades and all(n["bad"] for n in cascades))
        self.assertTrue(any("parent_view" in n["text"] for n in cascades))
        self.assertTrue(self.exists("parent") and self.exists("child"))

    def test_other_objects(self):
        view = self.preview("DROP VIEW pgdeck_w.parent_view")
        self.assertEqual((view["title"], view["headline"], view["headline_label"], view["objects"]), ("DROP VIEW?", "VIEW", "will be permanently removed.", []))
        schema = self.preview("DROP SCHEMA pgdeck_w CASCADE")
        self.assertEqual((schema["title"], schema["headline"]), ("DROP SCHEMA?", "SCHEMA"))
        self.assertTrue(any("cascades" in n["text"] for n in schema["notes"]))
        missing = self.preview("DROP TABLE IF EXISTS pgdeck_w.not_there")
        self.assertTrue(any("does not exist, skipping" in n["text"] for n in missing["notes"]))
        self.assertTrue(self.exists("parent"))                                   # nothing above changed anything
        self.assertEqual(self.preview("DROP SEQUENCE pgdeck_w.log_id_seq CASCADE")["title"], "DROP SEQUENCE?")

    def test_only_the_exact_word_drop_confirms(self):
        sql = "DROP TABLE pgdeck_w.logged"
        fingerprint = self.preview(sql)["fingerprint"]
        for wrong in ("drop", "Drop", "DROP ", " DROP", "DROPP", "TRUNCATE", "ZAP", "", None):
            with self.subTest(confirm=wrong), self.assertRaisesRegex(pg.PgError, "Type DROP in capital letters"):
                self.execute(sql, fingerprint, wrong)
        self.assertTrue(self.exists("logged"))
        self.assertFalse(AuditEntry.objects.exists())

    def test_confirmed_drop_removes_the_object_and_records_it_with_its_definition(self):
        sql = "DROP TABLE pgdeck_w.parent CASCADE"
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
        self.assertEqual(payload["tables"], [{"name": "pgdeck_w.parent", "rows": 10}])
        self.assertTrue(any("parent_view" in notice for notice in payload["notices"]))
        self.assertTrue(payload["ddl"][0]["sql"].startswith("CREATE TABLE pgdeck_w.parent ("))       # kept for reference, to recreate it
        self.assertFalse(entry.can_reinsert)
        self.assertEqual(entry.record_label, "DROP TABLE")
        self.assertIn("Dropped as asked (table)", result["message"])

    def test_dropping_a_schema_with_everything_in_it(self):
        with admin_connect(self.db) as conn:
            conn.execute("CREATE SCHEMA pgdeck_gone")
            conn.execute("CREATE TABLE pgdeck_gone.t (a int)")
            conn.execute("CREATE VIEW pgdeck_gone.v AS SELECT a FROM pgdeck_gone.t")
        fingerprint = self.preview("DROP SCHEMA pgdeck_gone CASCADE")["fingerprint"]
        self.assertTrue(fingerprint["notices"])
        result = self.execute("DROP SCHEMA pgdeck_gone CASCADE", fingerprint, "DROP")
        self.assertEqual(self.query("SELECT count(*) FROM pg_namespace WHERE nspname = 'pgdeck_gone'")[0][0], 0)
        self.assertEqual(AuditEntry.objects.get(pk=result["entry_id"]).payload["object_type"], "SCHEMA")

    def test_what_is_dropped_must_still_be_what_was_shown(self):
        sql = "DROP TABLE pgdeck_w.logged"
        fingerprint = self.preview(sql)["fingerprint"]
        with admin_connect(self.db) as conn:
            conn.execute("INSERT INTO pgdeck_w.logged VALUES (77, 'new data')")
        with self.assertRaisesRegex(pg.PgError, "changed since you checked"):
            self.execute(sql, fingerprint, "DROP")
        self.assertTrue(self.exists("logged"))
        self.assertEqual(self.count("logged"), 3)
        self.assertFalse(AuditEntry.objects.exists())

    def test_what_is_refused(self):
        for sql, message in (("DROP DATABASE pgdeck_w", "not available here"), ("DROP ROLE x", "not available here"),
                             ("DROP WIDGET w", "PgDeck can drop tables"), ("DROP TABLE pgdeck_w.a; DROP TABLE pgdeck_w.b", "one DROP statement"),
                             ("DROP TABLE pgdeck_w.nope", "does not exist")):
            with self.subTest(sql=sql), self.assertRaisesRegex(pg.PgError, message):
                self.preview(sql)


class ChangeEndpointTests(ChangeFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)

    def post(self, name, data, client=None):
        return (client or self.client).post(reverse(name, args=[self.server.pk]), json.dumps(data), content_type="application/json")

    def test_preview_endpoint_for_every_kind_that_has_one(self):
        for sql, kind, word in (("UPDATE pgdeck_w.parent SET note = 'x' WHERE id < 3", "update", None),
                                ("DELETE FROM pgdeck_w.parent WHERE id < 3", "delete", "ZAP"),
                                ("TRUNCATE pgdeck_w.ident", "truncate", "TRUNCATE"), ("DROP TABLE pgdeck_w.logged", "drop", "DROP")):
            with self.subTest(kind=kind):
                body = self.post("write_preview", {"sql": sql, "db": self.db}).json()
                self.assertEqual((body["ok"], body["kind"], body["confirm_word"]), (True, kind, word))
                for key in ("title", "headline", "headline_label", "warning", "notes", "fingerprint", "statement"):
                    self.assertIn(key, body)
        insert = self.post("write_preview", {"sql": "INSERT INTO pgdeck_w.logged VALUES (5, 'x')", "db": self.db})
        self.assertEqual(insert.status_code, 400)
        self.assertIn("nothing to preview", insert.json()["error"])
        self.assertEqual(self.count("parent"), 10)

    def test_insert_and_update_execute_without_a_word(self):
        insert = self.post("write_execute", {"sql": "INSERT INTO pgdeck_w.logged (id, what) VALUES (5, 'five')", "db": self.db})
        self.assertEqual((insert.status_code, insert.json()["ok"], insert.json()["count"]), (200, True, 1))
        preview = self.post("write_preview", {"sql": "UPDATE pgdeck_w.logged SET what = 'six' WHERE id = 5", "db": self.db}).json()
        update = self.post("write_execute", {"sql": "UPDATE pgdeck_w.logged SET what = 'six' WHERE id = 5", "db": self.db,
                                             "fingerprint": preview["fingerprint"]})
        self.assertEqual((update.status_code, update.json()["ok"], update.json()["count"]), (200, True, 1))
        self.assertEqual(self.query("SELECT what FROM pgdeck_w.logged WHERE id = 5")[0][0], "six")
        self.assertEqual([e.action for e in AuditEntry.objects.order_by("pk")], ["insert", "update"])

    def test_truncate_and_drop_need_their_own_switch(self):
        self.server.allow_structure = False
        self.server.save()
        for sql, word in (("TRUNCATE pgdeck_w.ident", "TRUNCATE"), ("DROP TABLE pgdeck_w.logged", "DROP")):
            for name in ("write_preview", "write_execute"):
                response = self.post(name, {"sql": sql, "db": self.db, "confirm": word, "fingerprint": {"tables": [], "notices": []}})
                self.assertEqual(response.status_code, 403, (sql, name))
                self.assertIn(f"{word} is switched off for this server", response.json()["error"])
                self.assertIn("Also allow TRUNCATE and DROP", response.json()["error"])
        self.assertEqual(self.count("ident"), 5)
        self.assertTrue(self.exists("logged"))
        # data changes themselves are still fine on this server
        self.assertEqual(self.post("write_preview", {"sql": "DELETE FROM pgdeck_w.ident WHERE id = 1", "db": self.db}).status_code, 200)

    def test_nothing_works_without_the_first_switch_or_a_staff_user(self):
        sql = "UPDATE pgdeck_w.parent SET note = 'x'"
        plain = Client()
        plain.force_login(get_user_model().objects.create_user("pat", password="pw-12345-abc"))
        for name in ("write_preview", "write_execute"):
            self.assertEqual(self.post(name, {"sql": sql, "db": self.db}, client=plain).status_code, 403)
        self.server.allow_writes = False
        self.server.allow_structure = True               # even with the second switch left on
        self.server.save()
        for sql in ("UPDATE pgdeck_w.parent SET note = 'x'", "INSERT INTO pgdeck_w.logged VALUES (8, 'x')", "TRUNCATE pgdeck_w.ident", "DROP TABLE pgdeck_w.logged"):
            for name in ("write_preview", "write_execute"):
                response = self.post(name, {"sql": sql, "db": self.db})
                self.assertEqual(response.status_code, 403, (sql, name))
                self.assertIn("Changing data is switched off", response.json()["error"])
        self.assertEqual(self.query("SELECT count(*) FROM pgdeck_w.parent WHERE note = 'x'")[0][0], 0)
        self.assertEqual((self.count("ident"), self.count("logged")), (5, 2))

    def test_a_refused_attempt_is_logged_under_its_own_kind(self):
        response = self.post("write_execute", {"sql": "TRUNCATE pgdeck_w.ident", "db": self.db, "confirm": "truncate",
                                               "fingerprint": {"tables": []}})
        self.assertEqual(response.status_code, 400)
        entry = AuditEntry.objects.get()
        self.assertEqual((entry.action, entry.ok), ("truncate", False))
        self.assertEqual(self.count("ident"), 5)

    def test_audit_log_page_shows_every_kind_of_record(self):
        self.execute("INSERT INTO pgdeck_w.logged (id, what) VALUES (30, 'x')")
        self.execute("UPDATE pgdeck_w.logged SET what = 'y' WHERE id = 30", {"count": 1, "side_effects": []})
        fingerprint = self.preview("TRUNCATE pgdeck_w.ident")["fingerprint"]
        self.execute("TRUNCATE pgdeck_w.ident", fingerprint, "TRUNCATE")
        fingerprint = self.preview("DROP VIEW pgdeck_w.parent_view")["fingerprint"]
        self.execute("DROP VIEW pgdeck_w.parent_view", fingerprint, "DROP")
        page = self.client.get(reverse("audit_list"))
        for text in ("INSERT · 1 row into pgdeck_w.logged", "UPDATE · 1 row in pgdeck_w.logged", "TRUNCATE · 1 table, 5 rows",
                     "DROP VIEW", "&quot;kind&quot;: &quot;truncate&quot;", "cannot be re-inserted from the audit log"):
            self.assertContains(page, text)
        self.assertNotContains(page, "Re-insert ")                                # only deletes can be put back

    def test_server_form_switches(self):
        page = self.client.get(reverse("server_edit", args=[self.server.pk]))
        for text in ("Allow changing data", "Also allow TRUNCATE and DROP", "INSERT, UPDATE and DELETE"):
            self.assertContains(page, text)

    def test_popup_markup_and_script_cover_all_statements(self):
        page = self.client.get(reverse("sql_runner", args=[self.server.pk]))
        for text in ('id="changeWarning"', 'id="changeObjects"', 'id="changeWord"', "INSERT</strong> runs at once", "UPDATE</strong> shows how many rows",
                     "type ZAP", "type TRUNCATE or DROP"):
            self.assertContains(page, text)
        script = open("static/write_flow.js", encoding="utf-8").read()
        for needle in ('kind === "insert"', "preview.confirm_word", "preview.fingerprint", "preview.objects", "preview.notes",
                       "(insert|update|delete|truncate|drop)", "tone(\"error\")", "tone(\"success\")"):
            self.assertIn(needle, script)
