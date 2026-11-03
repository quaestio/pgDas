"""Indexes: create, rename, drop and reindex from the Indexes tab, all through the review popup."""
import json
import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from audit.models import AuditEntry

from .tests import admin_connect
from .tests_admin import AdminFixture, DB

BASE_DIR = Path(__file__).resolve().parent.parent


class IndexFixture(AdminFixture, TestCase):
    def setUp(self):
        super().setUp()
        with admin_connect("postgres") as admin:
            admin.execute(f"CREATE DATABASE {DB}")
        with admin_connect(DB) as conn:
            conn.execute("CREATE TABLE people (id serial PRIMARY KEY, name text NOT NULL, email text, active boolean);"
                         "CREATE INDEX people_name ON people (name); INSERT INTO people (name, email, active) VALUES ('Ann', 'a@x', true), ('Bob', 'b@x', false)")
        self.client = Client()
        self.client.force_login(self.staff)

    def indexes(self):
        with admin_connect(DB) as conn:
            return [r[0] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'people' ORDER BY 1").fetchall()]

    def send(self, name, sql, **extra):
        return self.client.post(reverse(name, args=[self.server.pk]), json.dumps({"sql": sql, "db": DB, **extra}), content_type="application/json")

    def go(self, sql, confirm=None):
        preview = self.send("write_preview", sql)
        self.assertEqual(preview.status_code, 200, preview.content)
        data = preview.json()
        return data, self.send("write_execute", sql, fingerprint=data["fingerprint"], confirm=confirm)


class IndexStatementTests(IndexFixture):
    def test_create_a_composite_partial_expression_index(self):
        data, response = self.go('CREATE UNIQUE INDEX "people_email_active" ON "public"."people" USING btree ("name" DESC, (lower(email))) WHERE active')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("people_email_active", self.indexes())
        self.assertTrue(any("create index public.people_email_active" in o["text"] for o in data["objects"]))

    def test_create_without_a_name_lets_postgresql_pick_one(self):
        _, response = self.go('CREATE INDEX ON "public"."people" USING btree ("email")')
        self.assertEqual(response.status_code, 200)
        self.assertIn("people_email_idx", self.indexes())

    def test_rename_asks_for_the_word_alter_and_drop_asks_for_drop(self):
        data, response = self.go('ALTER INDEX "public"."people_name" RENAME TO "people_by_name"', confirm="ALTER")
        self.assertEqual((data["confirm_word"], response.status_code), ("ALTER", 200))
        self.assertIn("people_by_name", self.indexes())
        data, response = self.go('DROP INDEX "public"."people_by_name"', confirm="DROP")
        self.assertEqual((data["confirm_word"], response.status_code), ("DROP", 200))
        self.assertEqual(self.indexes(), ["people_pkey"])

    def test_dropping_the_primary_key_index_is_explained_by_postgresql(self):
        response = self.send("write_preview", 'DROP INDEX "public"."people_pkey"')
        self.assertEqual(response.status_code, 400)
        self.assertIn("constraint", response.json()["error"])


class ReindexTests(IndexFixture):
    def test_reindex_an_index_and_a_table_are_previewed_run_and_recorded(self):
        data, response = self.go('REINDEX INDEX "public"."people_name"')
        self.assertEqual((data["title"], data["count"], data["confirm_word"], data["button"]), ("REINDEX?", 1, None, "Reindex"))
        self.assertIn("rebuild index public.people_name", data["objects"][0]["text"])
        self.assertEqual(response.status_code, 200, response.content)
        data, response = self.go('REINDEX TABLE "public"."people"')
        self.assertEqual((data["count"], response.status_code), (2, 200))
        entries = AuditEntry.objects.filter(action="reindex").order_by("pk")
        self.assertEqual([e.record_label for e in entries], ["REINDEX · 1 index", "REINDEX · 2 indexes"])

    def test_wider_or_concurrent_forms_and_missing_names_are_refused(self):
        for sql, message in (('REINDEX INDEX CONCURRENTLY "people_name"', "CONCURRENTLY"), ("REINDEX DATABASE postgres", "not DATABASE"),
                             ("REINDEX SYSTEM postgres", "not DATABASE"), ('REINDEX INDEX "public"."nope"', "not found")):
            with self.subTest(sql=sql):
                response = self.send("write_preview", sql)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json()["error"])

    def test_the_run_stops_if_the_indexes_changed_after_the_preview(self):
        sql = 'REINDEX TABLE "public"."people"'
        data = self.send("write_preview", sql).json()
        with admin_connect(DB) as conn:
            conn.execute("CREATE INDEX people_email ON people (email)")
        response = self.send("write_execute", sql, fingerprint=data["fingerprint"])
        self.assertEqual(response.status_code, 400)
        self.assertIn("changed since you checked", response.json()["error"])
        self.assertFalse(AuditEntry.objects.filter(action="reindex", ok=True).exists())

    def test_only_staff_may_reindex(self):
        plain = Client()
        plain.force_login(get_user_model().objects.create_user("pat", password="pw-12345-abc"))
        response = plain.post(reverse("write_preview", args=[self.server.pk]), json.dumps({"sql": 'REINDEX TABLE "public"."people"', "db": DB}), content_type="application/json")
        self.assertEqual(response.status_code, 403)


class IndexTabTests(IndexFixture):
    def page(self, client=None):
        return (client or self.client).get(reverse("detail"), {"type": "table", "server": self.server.pk, "db": DB, "schema": "public", "table": "people", "tab": "indexes"}).content.decode()

    def test_staff_get_the_buttons_and_the_column_list(self):
        html = self.page()
        for needle in ('data-table-dialog="create-index"', 'data-table-dialog="reindex-table"', 'data-index="people_name"', 'data-index="people_pkey"',
                       'data-table-dialog="rename-index"', 'data-table-dialog="reindex-index"', 'data-table-dialog="drop-index"',
                       'data-index-tab="[&quot;id&quot;, &quot;name&quot;, &quot;email&quot;, &quot;active&quot;]"'):
            self.assertIn(needle, html)
        primary = re.search(r'<tr data-index="people_pkey">.*?</tr>', html, re.S).group(0)
        self.assertNotIn("drop-index", primary)                                         # the key's index is dropped as a constraint, not here

    def test_others_get_a_plain_list(self):
        plain = Client()
        plain.force_login(get_user_model().objects.create_user("pat", password="pw-12345-abc"))
        html = self.page(plain)
        self.assertNotIn("data-table-dialog", html)
        self.assertIn("people_name", html)

    def test_the_script_builds_each_statement(self):
        script = (BASE_DIR / "static" / "table_dialogs.js").read_text(encoding="utf-8")
        for needle in ('"create-index"', '"rename-index"', "CREATE \" + (v.unique", '"DROP INDEX "', '"REINDEX INDEX "', '"REINDEX TABLE "', "ALTER INDEX ", " USING "):
            self.assertIn(needle, script)
