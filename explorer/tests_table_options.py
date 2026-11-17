"""New table: access method, OF type, TOAST parameters, exclude constraints and constraint comments; constraints and indexes on partitioned tables."""
from pathlib import Path

from django.urls import reverse

from . import pg
from .tests import admin_connect
from .tests_admin import DB
from .tests_table_script import ScriptFixture

BASE_DIR = Path(__file__).resolve().parent.parent


class TableOptionsTests(ScriptFixture):
    def test_the_lookup_offers_access_methods_and_composite_types(self):
        with admin_connect(DB) as conn:
            conn.execute("CREATE TYPE point_pair AS (a int, b int); CREATE TYPE mood AS ENUM ('ok', 'bad')")
        data = self.client.get(reverse("column_types", args=[self.server.pk]), {"db": DB}).json()
        self.assertIn("heap", data["access_methods"])
        composites = {(c["schema"], c["name"]): c["fields"] for c in data["composites"]}
        self.assertEqual(composites, {("public", "point_pair"): ["a", "b"]})                  # an enum is not a composite type
        with pg.connect(self.server, DB) as conn:
            methods, found = pg.table_options(conn)
        self.assertEqual(methods[0], "heap")
        self.assertEqual(len(found), 1)

    def test_access_method_and_toast_parameters_are_applied(self):
        _, response = self.go('CREATE TABLE "public"."docs" (\n    "id" bigserial,\n    "body" text,\n    PRIMARY KEY ("id")\n) USING "heap" WITH (fillfactor = 80, toast.autovacuum_enabled = false, toast.autovacuum_vacuum_threshold = 100)')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.query("SELECT a.amname FROM pg_class c JOIN pg_am a ON a.oid = c.relam WHERE c.oid = 'docs'::regclass"), [("heap",)])
        options = self.query("SELECT t.reloptions::text FROM pg_class c JOIN pg_class t ON t.oid = c.reltoastrelid WHERE c.oid = 'docs'::regclass")[0][0]
        self.assertIn("autovacuum_enabled=false", options)
        self.assertIn("autovacuum_vacuum_threshold=100", options)

    def test_a_table_can_be_made_of_a_composite_type_with_a_key_and_a_comment_on_it(self):
        with admin_connect(DB) as conn:
            conn.execute("CREATE TYPE point_pair AS (a int, b int)")
        script = ('CREATE TABLE "public"."pairs" OF "public"."point_pair" (\n    PRIMARY KEY ("a"),\n    CONSTRAINT "b_positive" CHECK (b > 0)\n);\n'
                  'COMMENT ON CONSTRAINT "b_positive" ON "public"."pairs" IS \'b must be above zero\'')
        _, response = self.go(script)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.query("SELECT obj_description(oid, 'pg_constraint') FROM pg_constraint WHERE conname = 'b_positive'"), [("b must be above zero",)])
        self.assertEqual(self.query("SELECT reloftype::regtype::text FROM pg_class WHERE oid = 'pairs'::regclass"), [("point_pair",)])

    def test_a_comment_on_another_tables_constraint_is_not_accepted_as_a_follow_up(self):
        with admin_connect(DB) as conn:
            conn.execute("CREATE TABLE other (id int, CONSTRAINT keep PRIMARY KEY (id))")
        response = self.send("write_preview", 'CREATE TABLE "public"."mine" (id int);\nCOMMENT ON CONSTRAINT "keep" ON "public"."other" IS \'x\'')
        self.assertIn(response.status_code, (400, 403))
        self.assertFalse(self.query("SELECT 1 FROM pg_class WHERE relname = 'mine'"))

    def test_an_exclude_constraint_in_a_new_table_and_added_to_an_existing_one(self):
        _, response = self.go('CREATE TABLE "public"."booking" (\n    "room" int,\n    "during" tstzrange,\n    CONSTRAINT "no_overlap" EXCLUDE USING gist ("during" WITH &&)\n)')
        self.assertEqual(response.status_code, 200, response.content)
        with admin_connect(DB) as conn:
            conn.execute("INSERT INTO booking VALUES (1, '[2026-01-01, 2026-01-10)')")
            with self.assertRaises(Exception):
                conn.execute("INSERT INTO booking VALUES (1, '[2026-01-05, 2026-01-15)')")
        with admin_connect(DB) as conn:
            conn.execute("CREATE TABLE slots (id int, span tstzrange, active boolean)")
        _, added = self.go('ALTER TABLE "public"."slots" ADD CONSTRAINT "slots_apart" EXCLUDE USING gist ("span" WITH &&) WHERE (active)')
        self.assertEqual(added.status_code, 200, added.content)
        self.assertIn("EXCLUDE USING gist", self.query("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'slots_apart'")[0][0])

    def test_the_dialog_scripts_build_these_clauses(self):
        dialogs = (BASE_DIR / "static" / "table_dialogs.js").read_text(encoding="utf-8")
        for needle in ('"Access method"', '"Of type"', "data-roles", '" USING "', '" OF "', "toast_av_enabled", '"toast." + ', "COMMENT ON CONSTRAINT ", "comments: true", "extraMethods"):
            self.assertIn(needle, dialogs)
        constraints = (BASE_DIR / "static" / "table_constraints.js").read_text(encoding="utf-8")
        for needle in ('["EXCLUDE"', "EXCLUDE USING ", "data-cons-op", "data-cons-method", "data-cons-comment", "options.comments", "a comment needs a name"):
            self.assertIn(needle, constraints)
        for script in (dialogs, constraints):
            self.assertNotIn("innerHTML", script)


class PartitionedParentTests(ScriptFixture):
    def setUp(self):
        super().setUp()
        with admin_connect(DB) as conn:
            conn.execute("CREATE TABLE sales (id bigint, saledate date NOT NULL) PARTITION BY RANGE (saledate);"
                         "CREATE TABLE sales_2025 PARTITION OF sales FOR VALUES FROM ('2025-01-01') TO ('2026-01-01')")

    def detail(self, tab, table="sales"):
        return self.client.get(reverse("detail"), {"type": "table", "server": self.server.pk, "db": DB, "schema": "public", "table": table, "tab": tab}).content.decode()

    def test_constraints_and_indexes_can_be_added_to_a_partitioned_table(self):
        self.assertIn('data-constraint-action="add"', self.detail("constraints"))
        self.assertIn('data-table-dialog="create-index"', self.detail("indexes"))
        _, index = self.go('CREATE INDEX "sales_date" ON "public"."sales" USING btree ("saledate")')
        self.assertEqual(index.status_code, 200, index.content)
        _, check = self.go('ALTER TABLE "public"."sales" ADD CONSTRAINT "positive_id" CHECK (id > 0)')
        self.assertEqual(check.status_code, 200, check.content)
        self.assertTrue(self.query("SELECT 1 FROM pg_constraint WHERE conname = 'positive_id' AND conrelid = 'sales_2025'::regclass"))     # the partitions get it too
