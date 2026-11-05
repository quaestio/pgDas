"""Creating a table with its owner, comment and privileges (New table: General, Advanced and Security tabs), as one reviewed change."""
import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from pgdas.testing import TestCase

from . import changes, pg
from .tests import admin_connect
from .tests_admin import AdminFixture, DB, ROLE

BASE_DIR = Path(__file__).resolve().parent.parent
CREATE = 'CREATE TABLE "public"."orders" (\n    "id" bigserial,\n    "title" text NOT NULL,\n    PRIMARY KEY ("id")\n)'


class ScriptFixture(AdminFixture, TestCase):
    def setUp(self):
        super().setUp()
        with admin_connect("postgres") as admin:
            admin.execute(f"CREATE DATABASE {DB}")
        with admin_connect(DB) as conn:
            conn.execute(f"CREATE ROLE {ROLE}")
        self.client = Client()
        self.client.force_login(self.staff)

    def send(self, name, sql, client=None, **extra):
        return (client or self.client).post(reverse(name, args=[self.server.pk]), json.dumps({"sql": sql, "db": DB, **extra}), content_type="application/json")

    def go(self, sql, confirm=None):
        preview = self.send("write_preview", sql)
        self.assertEqual(preview.status_code, 200, preview.content)
        data = preview.json()
        return data, self.send("write_execute", sql, fingerprint=data["fingerprint"], confirm=confirm)

    def query(self, sql, *params):
        with admin_connect(DB) as conn:
            return conn.execute(sql, params).fetchall()


class ScriptTests(ScriptFixture):
    def test_a_table_is_created_with_owner_comment_and_privileges_in_one_review(self):
        script = (CREATE + ' WITH (fillfactor = 70);\nALTER TABLE "public"."orders" OWNER TO ' + ROLE + ";\nCOMMENT ON TABLE \"public\".\"orders\" IS 'it''s the orders';\n"
                  'COMMENT ON COLUMN "public"."orders"."title" IS \'what it is\';\nGRANT SELECT, INSERT ON TABLE "public"."orders" TO ' + ROLE + " WITH GRANT OPTION;\n"
                  'GRANT SELECT ON TABLE "public"."orders" TO PUBLIC')
        data, response = self.go(script)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((data["title"], data["confirm_word"]), ("CREATE TABLE?", None))
        self.assertEqual(sum(1 for n in data["notes"] if n["text"].startswith("Then: ")), 5)           # the follow-up statements are listed in the review
        self.assertEqual(self.query("SELECT pg_get_userbyid(relowner), obj_description(oid, 'pg_class'), reloptions::text FROM pg_class WHERE oid = 'orders'::regclass"),
                         [(ROLE, "it's the orders", "{fillfactor=70}")])
        self.assertEqual(self.query("SELECT col_description('orders'::regclass, 2)"), [("what it is",)])
        self.assertTrue(self.query("SELECT has_table_privilege(%s, 'orders', 'SELECT')", ROLE)[0][0])
        self.assertTrue(self.query("SELECT has_table_privilege('public', 'orders', 'SELECT')")[0][0])
        self.assertFalse(self.query("SELECT has_table_privilege('public', 'orders', 'DELETE')")[0][0])             # PUBLIC got SELECT only

    def test_unlogged_tables_work_and_the_audit_record_keeps_the_whole_script(self):
        _, response = self.go(CREATE.replace("CREATE TABLE", "CREATE UNLOGGED TABLE") + ';\nCOMMENT ON TABLE "public"."orders" IS \'x\'')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.query("SELECT relpersistence::text FROM pg_class WHERE oid = 'orders'::regclass"), [("u",)])
        from audit.models import AuditEntry
        entry = AuditEntry.objects.filter(action="create").latest("pk")
        self.assertIn("COMMENT ON TABLE", entry.payload["statement"])

    def test_nothing_else_can_ride_along_with_a_create_table(self):
        for extra, message in (('DROP TABLE "public"."other"', "one CREATE statement"),
                               ('ALTER TABLE "public"."orders" OWNER TO ' + ROLE + '; DROP TABLE x', "one CREATE statement"),
                               ('GRANT ALL ON TABLE "public"."elsewhere" TO PUBLIC', "for that same table"),
                               ('COMMENT ON TABLE "public"."elsewhere" IS \'x\'', "for that same table"),
                               ('GRANT SELECT ON TABLE "public"."orders" TO PUBLIC; DELETE FROM "public"."orders"', "one CREATE statement"),
                               ('INSERT INTO "public"."orders" ("title") VALUES (\'x\')', "one CREATE statement"),
                               ('GRANT CREATE ON TABLE "public"."orders" TO PUBLIC', "can give SELECT")):
            with self.subTest(extra=extra):
                response = self.send("write_preview", CREATE + ";\n" + extra)
                self.assertIn(response.status_code, (400, 403), response.content)      # refused by the engine (400) or already by the access policy (403)
                self.assertTrue(message in response.json()["error"] or "one statement" in response.json()["error"], response.json())
        self.assertEqual(self.query("SELECT count(*) FROM pg_class WHERE relname = 'orders'"), [(0,)])

    def test_two_creates_or_a_view_first_are_not_a_script(self):
        for sql in (CREATE + ";\n" + CREATE.replace("orders", "orders2"), 'CREATE VIEW "public"."v" AS SELECT 1;\nCOMMENT ON VIEW "public"."v" IS \'x\''):
            with self.subTest(sql=sql[:40]):
                self.assertIn(self.send("write_preview", sql).status_code, (400, 403))

    def test_a_missing_role_stops_the_whole_thing_and_creates_nothing(self):
        response = self.send("write_preview", CREATE + ';\nGRANT SELECT ON TABLE "public"."orders" TO nobody_here_xyz')
        self.assertEqual(response.status_code, 400)
        self.assertIn("nobody_here_xyz", response.json()["error"])
        self.assertEqual(self.query("SELECT count(*) FROM pg_class WHERE relname = 'orders'"), [(0,)])

    def test_the_script_is_split_without_breaking_quoted_semicolons(self):
        _, response = self.go(CREATE + ";\nCOMMENT ON TABLE \"public\".\"orders\" IS 'a; b -- not a comment'")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.query("SELECT obj_description('orders'::regclass, 'pg_class')"), [("a; b -- not a comment",)])

    def test_only_super_users_can_run_a_script_but_a_plain_create_is_unchanged(self):
        plain = Client()
        plain.force_login(get_user_model().objects.create_user("pat", password="pw-12345-abc"))
        self.assertIn(self.send("write_preview", CREATE + ';\nCOMMENT ON TABLE "public"."orders" IS \'x\'', client=plain).status_code, (403, 404))      # not allowed, or not even visible
        self.assertEqual(changes.table_script_statements(CREATE), [])
        self.assertEqual(len(changes.table_script_statements(CREATE + ';\nCOMMENT ON TABLE "public"."orders" IS \'x\'')), 2)


class DialogWiringTests(ScriptFixture):
    def test_the_types_endpoint_also_lists_the_roles_for_the_owner_and_privileges(self):
        data = self.client.get(reverse("column_types", args=[self.server.pk]), {"db": DB}).json()
        self.assertIn(ROLE, data["roles"])
        self.assertNotIn("pg_read_all_data", data["roles"])

    def test_the_new_table_dialog_has_every_tab_and_builds_the_statements(self):
        script = (BASE_DIR / "static" / "table_dialogs.js").read_text(encoding="utf-8")
        for needle in ('["advanced", "Advanced"]', '["security", "Security"]', "UNLOGGED ", "fillfactor = ", "OWNER TO ", "COMMENT ON TABLE ", "GRANT ", "WITH GRANT OPTION",
                       "ALL PRIVILEGES", "data-priv-add", "fillRoles", "the login PGDAS uses"):
            self.assertIn(needle, script)
        self.assertNotIn("innerHTML", script)


class MoreOptionsTests(ScriptFixture):
    def setUp(self):
        super().setUp()
        with admin_connect(DB) as conn:
            conn.execute("CREATE TABLE base (id serial PRIMARY KEY, note text DEFAULT 'n'); COMMENT ON COLUMN base.note IS 'about'")

    def columns(self, table):
        return [r[0] for r in self.query("SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped ORDER BY attnum", table)]

    def test_like_storage_parameters_tablespace_and_row_level_security(self):
        script = ('CREATE TABLE "public"."copy" (\n    LIKE "public"."base" INCLUDING DEFAULTS INCLUDING COMMENTS,\n    "n" int\n) '
                  "WITH (fillfactor = 70, toast_tuple_target = 256, parallel_workers = 2, autovacuum_enabled = false, autovacuum_vacuum_scale_factor = 0.05) "
                  'TABLESPACE "pg_default";\nALTER TABLE "public"."copy" ENABLE ROW LEVEL SECURITY;\nALTER TABLE "public"."copy" FORCE ROW LEVEL SECURITY')
        _, response = self.go(script)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.columns("copy"), ["id", "note", "n"])
        options = self.query("SELECT reloptions::text FROM pg_class WHERE oid = 'copy'::regclass")[0][0]
        for part in ("fillfactor=70", "toast_tuple_target=256", "parallel_workers=2", "autovacuum_enabled=false", "autovacuum_vacuum_scale_factor=0.05"):
            self.assertIn(part, options)
        self.assertEqual(self.query("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = 'copy'::regclass"), [(True, True)])
        self.assertEqual(self.query("SELECT col_description('copy'::regclass, 2)"), [("about",)])

    def test_inherits_adds_the_parents_columns(self):
        _, response = self.go('CREATE TABLE "public"."kid" (\n    "x" int\n) INHERITS ("public"."base")')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.columns("kid"), ["id", "note", "x"])
        self.assertEqual(self.query("SELECT count(*) FROM pg_inherits WHERE inhrelid = 'kid'::regclass AND inhparent = 'base'::regclass"), [(1,)])

    def test_row_level_security_for_another_table_is_not_allowed_as_a_follow_up(self):
        response = self.send("write_preview", CREATE + ';\nALTER TABLE "public"."base" ENABLE ROW LEVEL SECURITY')
        self.assertIn(response.status_code, (400, 403))
        self.assertEqual(self.query("SELECT relrowsecurity FROM pg_class WHERE oid = 'base'::regclass"), [(False,)])

    def test_the_lookup_lists_tablespaces_and_the_dialog_builds_the_new_clauses(self):
        data = self.client.get(reverse("column_types", args=[self.server.pk]), {"db": DB}).json()
        self.assertIn("pg_default", data["tablespaces"])
        script = (BASE_DIR / "static" / "table_dialogs.js").read_text(encoding="utf-8")
        for needle in ('["parameters", "Parameters"]', "INHERITS (", '" TABLESPACE "', "ENABLE ROW LEVEL SECURITY", "FORCE ROW LEVEL SECURITY", "toast_tuple_target",
                       "parallel_workers", "autovacuum_vacuum_scale_factor", " INCLUDING ", "LIKE_OPTIONS", "fillTables"):
            self.assertIn(needle, script)
        self.assertNotIn("innerHTML", script)


class PartitionTests(ScriptFixture):
    def parts(self, parent):
        return sorted(r[0] for r in self.query("SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = %s::regclass", parent))

    def test_a_range_partitioned_table_with_partitions_and_a_default(self):
        script = ('CREATE TABLE "public"."sales" (\n    "id" bigint,\n    "saledate" date NOT NULL\n) PARTITION BY RANGE ("saledate");\n'
                  'CREATE TABLE "public"."sales_2024" PARTITION OF "public"."sales" FOR VALUES FROM (\'2024-01-01\') TO (\'2025-01-01\');\n'
                  'CREATE TABLE "public"."sales_2025" PARTITION OF "public"."sales" FOR VALUES FROM (\'2025-01-01\') TO (MAXVALUE);\n'
                  'CREATE TABLE "public"."sales_other" PARTITION OF "public"."sales" DEFAULT;\n'
                  'COMMENT ON TABLE "public"."sales" IS \'by year\'')
        data, response = self.go(script)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.parts("sales"), ["sales_2024", "sales_2025", "sales_other"])
        self.assertEqual(self.query("SELECT relkind::text FROM pg_class WHERE oid = 'sales'::regclass"), [("p",)])
        with admin_connect(DB) as conn:
            conn.execute("INSERT INTO sales VALUES (1, '2024-06-01'), (2, '2025-02-02'), (3, '1999-01-01')")
        self.assertEqual(self.query("SELECT count(*) FROM sales_2024"), [(1,)])
        self.assertEqual(self.query("SELECT count(*) FROM sales_other"), [(1,)])
        self.assertTrue(any("partitioned table public.sales" in o["text"] for o in data["objects"]))

    def test_list_and_hash_partitions(self):
        _, response = self.go('CREATE TABLE "public"."regions" ("code" text, "n" int) PARTITION BY LIST ("code");\n'
                              'CREATE TABLE "public"."regions_n" PARTITION OF "public"."regions" FOR VALUES IN (\'north\', \'it\'\'s\');\n'
                              'CREATE TABLE "public"."regions_rest" PARTITION OF "public"."regions" DEFAULT')
        self.assertEqual(response.status_code, 200, response.content)
        _, response = self.go('CREATE TABLE "public"."hashed" ("id" int) PARTITION BY HASH ("id");\n'
                              'CREATE TABLE "public"."hashed_0" PARTITION OF "public"."hashed" FOR VALUES WITH (MODULUS 2, REMAINDER 0);\n'
                              'CREATE TABLE "public"."hashed_1" PARTITION OF "public"."hashed" FOR VALUES WITH (MODULUS 2, REMAINDER 1)')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.parts("hashed"), ["hashed_0", "hashed_1"])

    def test_a_partition_key_expression_works(self):
        _, response = self.go('CREATE TABLE "public"."byyear" ("saledate" date) PARTITION BY RANGE ((extract(year from "saledate")));\n'
                              'CREATE TABLE "public"."byyear_24" PARTITION OF "public"."byyear" FOR VALUES FROM (2024) TO (2025)')
        self.assertEqual(response.status_code, 200, response.content)

    def test_only_plain_values_and_only_this_tables_partitions_are_accepted(self):
        parent = 'CREATE TABLE "public"."sales" ("d" date) PARTITION BY RANGE ("d")'
        for child, message in (('CREATE TABLE "public"."c" PARTITION OF "public"."sales" FOR VALUES FROM (now()) TO (\'2030-01-01\')', "plain values"),
                               ('CREATE TABLE "public"."c" PARTITION OF "public"."sales" FOR VALUES FROM ((SELECT 1)) TO (2)', "plain values"),
                               ('CREATE TABLE "public"."c" PARTITION OF "public"."elsewhere" DEFAULT', "plain values"),
                               ('CREATE TABLE "public"."sales" PARTITION OF "public"."sales" DEFAULT', "plain values"),
                               ('CREATE TABLE "public"."c" (a int)', "one CREATE statement")):
            with self.subTest(child=child):
                response = self.send("write_preview", parent + ";\n" + child)
                self.assertIn(response.status_code, (400, 403), response.content)
                self.assertTrue(message in response.json()["error"] or "one statement" in response.json()["error"], response.json())
        self.assertEqual(self.query("SELECT count(*) FROM pg_class WHERE relname IN ('sales', 'c')"), [(0,)])

    def test_overlapping_partitions_are_reported_by_postgresql_and_create_nothing(self):
        response = self.send("write_preview", 'CREATE TABLE "public"."sales" ("d" date) PARTITION BY RANGE ("d");\n'
                             'CREATE TABLE "public"."a" PARTITION OF "public"."sales" FOR VALUES FROM (\'2024-01-01\') TO (\'2025-01-01\');\n'
                             'CREATE TABLE "public"."b" PARTITION OF "public"."sales" FOR VALUES FROM (\'2024-06-01\') TO (\'2026-01-01\')')
        self.assertEqual(response.status_code, 400)
        self.assertIn("overlap", response.json()["error"])
        self.assertEqual(self.query("SELECT count(*) FROM pg_class WHERE relname = 'sales'"), [(0,)])

    def test_the_dialog_has_the_partitions_tab_and_builds_the_statements(self):
        script = (BASE_DIR / "static" / "table_dialogs.js").read_text(encoding="utf-8")
        for needle in ('["partitions", "Partitions"]', "PARTITION BY ", " PARTITION OF ", "FOR VALUES FROM (", "FOR VALUES IN (", "FOR VALUES WITH (MODULUS ", "DEFAULT",
                       "partitionsProblem", "data-part-add", "data-pk-add", "list partitioning takes exactly one key", "Partitioned table?"):
            self.assertIn(needle, script)
        self.assertNotIn("innerHTML", script)
