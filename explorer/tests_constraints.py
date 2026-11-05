"""Constraints: New table's Constraints tab, and Add / Rename / Drop constraint on a table's Constraints tab."""
import json
import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client
from pgdas.testing import TestCase
from django.urls import reverse

from .tests import admin_connect
from .tests_admin import AdminFixture, DB

BASE_DIR = Path(__file__).resolve().parent.parent


class ConstraintFixture(AdminFixture, TestCase):
    def setUp(self):
        super().setUp()
        with admin_connect("postgres") as admin:
            admin.execute(f"CREATE DATABASE {DB}")
        with admin_connect(DB) as conn:
            conn.execute("CREATE TABLE parents (id int PRIMARY KEY, name text); CREATE TABLE kids (id int, parent_id int, age int);"
                         "INSERT INTO parents VALUES (1, 'a'), (2, 'b'); INSERT INTO kids VALUES (1, 1, 5), (2, 2, 7), (3, 99, -1);"
                         "CREATE VIEW parent_names AS SELECT name FROM parents; CREATE SCHEMA other; CREATE TABLE other.places (code text PRIMARY KEY)")
        self.client = Client()
        self.client.force_login(self.staff)

    def send(self, name, sql, **extra):
        return self.client.post(reverse(name, args=[self.server.pk]), json.dumps({"sql": sql, "db": DB, **extra}), content_type="application/json")

    def go(self, sql, confirm=None):
        preview = self.send("write_preview", sql)
        self.assertEqual(preview.status_code, 200, preview.content)
        data = preview.json()
        return data, self.send("write_execute", sql, fingerprint=data["fingerprint"], confirm=confirm)

    def constraints(self, table):
        with admin_connect(DB) as conn:
            return {r[0]: (r[1], r[2]) for r in conn.execute(
                "SELECT conname, contype::text, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = %s::regclass AND contype <> 'n'", (table,)).fetchall()}


class StatementTests(ConstraintFixture):
    """The SQL static/table_constraints.js writes, run through the same review popup path as every change."""

    def test_primary_key_unique_check_and_foreign_key_can_be_added(self):
        for sql in ('ALTER TABLE "public"."kids" ADD CONSTRAINT "kids_ok" CHECK (age > -5)',
                    'ALTER TABLE "public"."kids" ADD UNIQUE ("id")',
                    'ALTER TABLE "public"."kids" ADD PRIMARY KEY ("id")'):
            with self.subTest(sql=sql):
                data, response = self.go(sql)
                self.assertEqual(response.status_code, 200, response.content)
        found = self.constraints("kids")
        self.assertEqual({kind for kind, _ in found.values()}, {"c", "u", "p"})

    def test_a_foreign_key_with_actions_and_a_composite_key(self):
        self.go('ALTER TABLE "public"."kids" DELETE_PLACEHOLDER'.replace(' DELETE_PLACEHOLDER', ' DROP COLUMN "age"'), confirm="ALTER")
        data, response = self.go('ALTER TABLE "public"."kids" ADD CONSTRAINT "kid_parent" FOREIGN KEY ("parent_id") REFERENCES "public"."parents" ("id") '
                                 'ON DELETE CASCADE ON UPDATE NO ACTION NOT VALID', confirm="ALTER")      # CASCADE is a risky word: it asks for ALTER
        self.assertEqual((data["confirm_word"], response.status_code), ("ALTER", 200), response.content)
        kind, definition = self.constraints("kids")["kid_parent"]
        self.assertEqual(kind, "f")
        self.assertIn("ON DELETE CASCADE", definition)
        self.assertIn("NOT VALID", definition)                                           # the rows already there (parent 99 is missing) were not checked
        with admin_connect(DB) as conn:
            conn.execute("DELETE FROM parents WHERE id = 1")
            self.assertEqual(conn.execute("SELECT count(*) FROM kids WHERE parent_id = 1").fetchone()[0], 0)    # cascade works

    def test_a_foreign_key_to_a_table_in_another_schema(self):
        with admin_connect(DB) as conn:
            conn.execute("ALTER TABLE kids ADD COLUMN place text")
        _, response = self.go('ALTER TABLE "public"."kids" ADD FOREIGN KEY ("place") REFERENCES "other"."places" ("code")')
        self.assertEqual(response.status_code, 200, response.content)

    def test_data_that_breaks_the_constraint_is_refused_with_the_reason_and_nothing_changes(self):
        for sql, message in (('ALTER TABLE "public"."kids" ADD CHECK (age > 0)', "check constraint"),
                             ('ALTER TABLE "public"."kids" ADD FOREIGN KEY ("parent_id") REFERENCES "public"."parents" ("id")', "foreign key constraint"),
                             ('ALTER TABLE "public"."kids" ADD PRIMARY KEY ("parent_id", "age", "id", "nope")', "nope")):
            with self.subTest(sql=sql):
                response = self.send("write_preview", sql)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json()["error"])
        self.assertEqual(self.constraints("kids"), {})

    def test_drop_and_rename_ask_for_the_word_alter(self):
        self.go('ALTER TABLE "public"."kids" ADD CONSTRAINT "kids_ok" CHECK (age > -5)')
        data, response = self.go('ALTER TABLE "public"."kids" RENAME CONSTRAINT "kids_ok" TO "age_floor"', confirm="ALTER")
        self.assertEqual((data["confirm_word"], response.status_code), ("ALTER", 200))
        self.assertIn("age_floor", self.constraints("kids"))
        data, response = self.go('ALTER TABLE "public"."kids" DROP CONSTRAINT "age_floor"', confirm="ALTER")
        self.assertEqual((data["confirm_word"], response.status_code), ("ALTER", 200))
        self.assertEqual(self.constraints("kids"), {})
        refused = self.send("write_execute", 'ALTER TABLE "public"."parents" DROP CONSTRAINT "parents_pkey"', fingerprint={"changes": []}, confirm="no")
        self.assertEqual(refused.status_code, 400)

    def test_create_table_with_every_kind_of_constraint(self):
        sql = ('CREATE TABLE "public"."orders" (\n    "id" bigserial,\n    "parent_id" integer NOT NULL,\n    "qty" integer,\n    "code" text,\n'
               '    PRIMARY KEY ("id"),\n    CONSTRAINT "by_parent" FOREIGN KEY ("parent_id") REFERENCES "public"."parents" ("id") ON DELETE RESTRICT,\n'
               '    UNIQUE ("code"),\n    CONSTRAINT "qty_positive" CHECK (qty > 0)\n)')
        _, response = self.go(sql)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual({k: v[0] for k, v in self.constraints("orders").items() if k in ("orders_pkey", "by_parent", "orders_code_key", "qty_positive")},
                         {"orders_pkey": "p", "by_parent": "f", "orders_code_key": "u", "qty_positive": "c"})


class TablesEndpointTests(ConstraintFixture):
    def get(self, **params):
        return self.client.get(reverse("database_tables", args=[self.server.pk]), {"db": DB, **params})

    def test_lists_ordinary_tables_with_their_columns_and_nothing_else(self):
        tables = {f"{t['schema']}.{t['name']}": t["columns"] for t in self.get().json()["tables"]}
        self.assertEqual(sorted(tables), ["other.places", "public.kids", "public.parents"])         # no view, no system schemas
        self.assertEqual(tables["public.parents"], [{"name": "id", "type": "integer"}, {"name": "name", "type": "text"}])

    def test_an_unreachable_database_is_an_error_not_a_crash(self):
        response = self.get(db="no_such_db_xyz")
        self.assertEqual(response.status_code, 502)
        self.assertIn("error", response.json())

    def test_the_structure_attribute_carries_the_address(self):
        html = self.client.get(reverse("detail"), {"type": "tables", "server": self.server.pk, "db": DB, "schema": "public"}).content.decode()
        meta = json.loads(re.search(r'data-structure="([^"]*)"', html).group(1).replace("&quot;", '"'))
        self.assertEqual(meta["tables_url"], reverse("database_tables", args=[self.server.pk]) + f"?db={DB}")


class ConstraintTabTests(ConstraintFixture):
    def tab(self, client=None):
        return (client or self.client).get(reverse("detail"), {"type": "table", "server": self.server.pk, "db": DB, "schema": "public", "table": "parents", "tab": "constraints"})

    def test_staff_get_add_rename_and_drop(self):
        html = self.tab().content.decode()
        for needle in ('data-constraint-action="add"', 'data-constraint-action="rename"', 'data-constraint-action="drop"', 'data-constraint="parents_pkey"',
                       'data-cons-tab="[&quot;id&quot;, &quot;name&quot;]"'):
            self.assertIn(needle, html)

    def test_people_with_no_role_get_nothing(self):
        plain = Client()
        plain.force_login(get_user_model().objects.create_user("pat", password="pw-12345-abc"))
        response = self.tab(plain)
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "data-constraint-action", status_code=404)

    def test_the_scripts_are_loaded_in_order_and_build_every_kind_safely(self):
        home = self.client.get("/").content.decode()
        self.assertLess(home.index("table_constraints.js"), home.index("table_dialogs.js"))
        script = (BASE_DIR / "static" / "table_constraints.js").read_text(encoding="utf-8")
        for needle in ('"PRIMARY KEY"', '"FOREIGN KEY"', '"UNIQUE"', '"CHECK"', "REFERENCES ", " ON DELETE ", " ON UPDATE ", " NOT VALID", " ADD ",
                       "RENAME CONSTRAINT ", "DROP CONSTRAINT ", "pgdasReview.start", "A table has only one primary key."):
            self.assertIn(needle, script)
        self.assertNotIn("innerHTML", script)
        self.assertNotRegex(script, r"\bconfirm\(")
        dialogs = (BASE_DIR / "static" / "table_dialogs.js").read_text(encoding="utf-8")
        for needle in ('["constraints", "Constraints"]', "pgdasConstraints.editor", "pgdasConstraints.problem", "pgdasConstraints.sql"):
            self.assertIn(needle, dialogs)
