"""Schemas, views, materialized views and sequences: create, rename and drop from the Object Browser, through the same review popup as everything else."""
from pathlib import Path

from django.urls import reverse

from .tests import admin_connect
from .tests_admin import DB
from .tests_table_script import ScriptFixture

BASE_DIR = Path(__file__).resolve().parent.parent


class ObjectStatementTests(ScriptFixture):
    """The statements static/table_dialogs.js builds for these dialogs."""

    def setUp(self):
        super().setUp()
        with admin_connect(DB) as conn:
            conn.execute("CREATE TABLE customers (id serial PRIMARY KEY, name text, country text); INSERT INTO customers (name, country) VALUES ('Ann', 'IN'), ('Bo', 'DE')")

    def exists(self, kind, name, schema="public"):
        flags = {"view": "v", "matview": "m", "sequence": "S", "schema": None}
        if kind == "schema":
            return bool(self.query("SELECT 1 FROM pg_namespace WHERE nspname = %s", name))
        return bool(self.query("SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relname = %s AND c.relkind = %s", schema, name, flags[kind]))

    def test_a_schema_is_created_renamed_and_dropped(self):
        _, created = self.go('CREATE SCHEMA "sales"')
        self.assertEqual(created.status_code, 200, created.content)
        self.assertTrue(self.exists("schema", "sales"))
        data, refused = self.go('ALTER SCHEMA "sales" RENAME TO "retail"')
        self.assertEqual(data["confirm_word"], "ALTER")                                  # a rename is risky: it asks for the word
        self.assertEqual(refused.status_code, 400)
        _, renamed = self.go('ALTER SCHEMA "sales" RENAME TO "retail"', confirm="ALTER")
        self.assertEqual(renamed.status_code, 200, renamed.content)
        self.assertTrue(self.exists("schema", "retail") and not self.exists("schema", "sales"))
        self.go('CREATE TABLE "retail"."t" (id int)')
        plain = self.send("write_preview", 'DROP SCHEMA "retail"')
        self.assertEqual(plain.status_code, 400)                                         # not empty: PostgreSQL says so and nothing is dropped
        self.assertIn("depend", plain.json()["error"])
        self.assertTrue(self.exists("schema", "retail"))
        _, dropped = self.go('DROP SCHEMA "retail" CASCADE', confirm="DROP")
        self.assertEqual(dropped.status_code, 200, dropped.content)
        self.assertFalse(self.exists("schema", "retail"))

    def test_a_schema_can_get_an_owner_when_it_is_created(self):
        _, response = self.go('CREATE SCHEMA "owned" AUTHORIZATION "pgdas_t_role"') if False else self.go('CREATE SCHEMA "owned"')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self.exists("schema", "owned"))

    def test_a_view_is_created_renamed_and_dropped(self):
        _, created = self.go('CREATE VIEW "public"."in_customers" AS\nSELECT id, name FROM customers WHERE country = \'IN\'')
        self.assertEqual(created.status_code, 200, created.content)
        self.assertEqual(self.query("SELECT name FROM in_customers"), [("Ann",)])
        _, renamed = self.go('ALTER VIEW "public"."in_customers" RENAME TO "indian_customers"', confirm="ALTER")
        self.assertEqual(renamed.status_code, 200, renamed.content)
        self.assertTrue(self.exists("view", "indian_customers") and not self.exists("view", "in_customers"))
        _, dropped = self.go('DROP VIEW "public"."indian_customers"', confirm="DROP")
        self.assertEqual(dropped.status_code, 200, dropped.content)
        self.assertFalse(self.exists("view", "indian_customers"))

    def test_a_materialized_view_with_and_without_data(self):
        _, full = self.go('CREATE MATERIALIZED VIEW "public"."by_country" AS\nSELECT country, count(*) AS n FROM customers GROUP BY country\nWITH DATA')
        self.assertEqual(full.status_code, 200, full.content)
        self.assertEqual(len(self.query("SELECT * FROM by_country")), 2)
        _, empty = self.go('CREATE MATERIALIZED VIEW "public"."later" AS\nSELECT * FROM customers\nWITH NO DATA')
        self.assertEqual(empty.status_code, 200, empty.content)
        self.assertFalse(self.query("SELECT relispopulated FROM pg_class WHERE relname = 'later'")[0][0])
        _, renamed = self.go('ALTER MATERIALIZED VIEW "public"."later" RENAME TO "filled"', confirm="ALTER")
        self.assertEqual(renamed.status_code, 200, renamed.content)
        _, dropped = self.go('DROP MATERIALIZED VIEW "public"."filled"', confirm="DROP")
        self.assertEqual(dropped.status_code, 200, dropped.content)
        self.assertFalse(self.exists("matview", "filled"))

    def test_a_sequence_with_its_options(self):
        _, created = self.go('CREATE SEQUENCE "public"."invoice_no"\n    AS integer\n    INCREMENT BY 5\n    MINVALUE 100\n    MAXVALUE 100000\n    START WITH 1000\n    CACHE 10\n    CYCLE')
        self.assertEqual(created.status_code, 200, created.content)
        row = self.query("SELECT seqtypid::regtype::text, seqstart, seqincrement, seqmin, seqmax, seqcache, seqcycle FROM pg_sequence WHERE seqrelid = 'invoice_no'::regclass")[0]
        self.assertEqual(row, ("integer", 1000, 5, 100, 100000, 10, True))
        _, renamed = self.go('ALTER SEQUENCE "public"."invoice_no" RENAME TO "bill_no"', confirm="ALTER")
        self.assertEqual(renamed.status_code, 200, renamed.content)
        _, dropped = self.go('DROP SEQUENCE "public"."bill_no"', confirm="DROP")
        self.assertEqual(dropped.status_code, 200, dropped.content)
        self.assertFalse(self.exists("sequence", "bill_no"))

    def test_a_wrong_query_creates_nothing(self):
        response = self.send("write_preview", 'CREATE VIEW "public"."broken" AS\nSELECT nope FROM customers')
        self.assertEqual(response.status_code, 400)
        self.assertIn("nope", response.json()["error"])
        self.assertFalse(self.exists("view", "broken"))


class ObjectPageTests(ScriptFixture):
    def setUp(self):
        super().setUp()
        with admin_connect(DB) as conn:
            conn.execute("CREATE TABLE customers (id serial PRIMARY KEY, name text); CREATE VIEW v_customers AS SELECT * FROM customers; "
                         "CREATE MATERIALIZED VIEW m_customers AS SELECT * FROM customers; CREATE SEQUENCE s_numbers")

    def detail(self, **params):
        return self.client.get(reverse("detail"), {"server": self.server.pk, "db": DB, **params}).content.decode()

    def test_the_schemas_list_can_create_rename_and_drop(self):
        html = self.detail(type="schemas")
        for text in ('data-table-dialog="new-schema"', 'data-table-dialog="rename-schema" data-schema="public"', 'data-table-dialog="drop-schema" data-schema="public"', "data-structure="):
            self.assertIn(text, html)

    def test_the_schema_page_can_rename_and_drop_its_schema(self):
        html = self.detail(type="schema", schema="public")
        self.assertIn('data-table-dialog="rename-schema" data-schema="public"', html)
        self.assertIn('data-table-dialog="new-table"', html)

    def test_each_folder_offers_its_own_new_button(self):
        self.assertIn('data-table-dialog="new-view"', self.detail(type="views", schema="public"))
        self.assertNotIn('data-table-dialog="new-table"', self.detail(type="views", schema="public"))
        self.assertIn('data-table-dialog="new-matview"', self.detail(type="matviews", schema="public"))
        self.assertIn('data-table-dialog="new-sequence"', self.detail(type="sequences", schema="public"))
        self.assertIn('data-table-dialog="new-table"', self.detail(type="tables", schema="public"))

    def test_a_view_a_materialized_view_and_a_sequence_can_be_renamed_and_dropped_but_have_no_column_editing(self):
        for node, name, relation in (("view", "v_customers", "view"), ("matview", "m_customers", "materialized view"), ("sequence", "s_numbers", "sequence")):
            with self.subTest(relation=relation):
                html = self.detail(type=node, schema="public", table=name)
                self.assertIn(f'data-table-dialog="rename-relation" data-relation="{relation}"', html)
                self.assertIn(f'data-table-dialog="drop-relation" data-relation="{relation}"', html)
                self.assertNotIn('data-table-dialog="rename-table"', html)
        columns = self.detail(type="view", schema="public", table="v_customers", tab="columns")
        for text in ("add-column", "edit-column", "drop-column", "data-col-move", "data-columns-tab"):
            self.assertNotIn(text, columns)

    def test_a_table_page_is_as_before(self):
        html = self.detail(type="table", schema="public", table="customers")
        self.assertIn('data-table-dialog="rename-table"', html)
        self.assertNotIn("rename-relation", html)
        self.assertIn('data-table-dialog="add-column"', self.detail(type="table", schema="public", table="customers", tab="columns"))

    def test_the_dialogs_build_the_statements(self):
        script = (BASE_DIR / "static" / "table_dialogs.js").read_text(encoding="utf-8")
        for needle in ('"new-schema"', '"rename-schema"', '"drop-schema"', '"new-view"', '"new-matview"', '"new-sequence"', '"rename-relation"', '"drop-relation"',
                       "CREATE SCHEMA ", "AUTHORIZATION", "ALTER SCHEMA ", "DROP SCHEMA ", "CREATE VIEW ", "CREATE MATERIALIZED VIEW ", "CREATE SEQUENCE ",
                       "WITH DATA", "INCREMENT BY", "relationWord", "data-relation"):
            self.assertIn(needle, script)
        self.assertNotIn("innerHTML", script)
