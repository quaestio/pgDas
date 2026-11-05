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
