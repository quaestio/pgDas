"""SQL runner extras: saved statements, hints by alias, the recent-statements list following a run; undoing an attach or a detach of a partition."""
import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from audit.models import AuditEntry

from .models import SavedQuery
from .tests import admin_connect
from .tests_admin import DB
from .tests_table_script import ScriptFixture

BASE_DIR = Path(__file__).resolve().parent.parent


class SavedStatementTests(ScriptFixture):
    def url(self):
        return reverse("sql_saved", args=[self.server.pk])

    def save(self, name, sql, client=None):
        return (client or self.client).post(self.url(), json.dumps({"name": name, "sql": sql}), content_type="application/json")

    def test_a_statement_is_saved_listed_replaced_and_deleted(self):
        first = self.save("Orders by country", "SELECT country, count(*) FROM orders GROUP BY 1").json()
        self.assertTrue(first["ok"])
        self.assertEqual([q["name"] for q in self.client.get(self.url()).json()["queries"]], ["Orders by country"])
        again = self.save("  Orders   by country ", "SELECT 1").json()                       # the same name (spaces ignored) replaces it
        self.assertEqual(len(again["queries"]), 1)
        self.assertEqual(again["queries"][0]["sql"], "SELECT 1")
        self.save("Another", "SELECT 2")
        self.assertEqual([q["name"] for q in self.client.get(self.url()).json()["queries"]], ["Another", "Orders by country"])
        deleted = self.client.post(reverse("sql_saved_delete", args=[self.server.pk, first["query"]["id"]]))
        self.assertTrue(deleted.json()["ok"])
        self.assertEqual([q["name"] for q in self.client.get(self.url()).json()["queries"]], ["Another"])

    def test_a_name_and_a_statement_are_needed(self):
        for name, sql in (("", "SELECT 1"), ("   ", "SELECT 1"), ("x" * 81, "SELECT 1"), ("ok", ""), ("ok", "   ")):
            with self.subTest(name=name[:10], sql=sql):
                response = self.save(name, sql)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["ok"])
        self.assertEqual(SavedQuery.objects.count(), 0)

    def test_saved_statements_are_personal(self):
        mine = self.save("Mine", "SELECT 'mine'").json()["query"]
        other = get_user_model().objects.create_user("pat", email="pat@o.test", password="pw-12345-abc", is_staff=True, email_verified=True)
        from access_control.models import Membership
        Membership.objects.get_or_create(user=other, defaults={"organization": Membership.objects.exclude(user=other).first().organization, "role": "super_user"})
        client = Client()
        client.force_login(other)
        self.assertEqual(client.get(self.url()).json()["queries"], [])
        self.assertEqual(client.post(reverse("sql_saved_delete", args=[self.server.pk, mine["id"]])).status_code, 404)
        self.assertTrue(SavedQuery.objects.filter(pk=mine["id"]).exists())

    def test_the_page_and_scripts_have_the_saved_list_and_the_history_follows_a_run(self):
        page = self.client.get(reverse("sql_runner", args=[self.server.pk]), {"db": DB})
        for needle in ('id="sqlSavedSection"', 'id="sqlSaveName"', 'id="sqlHistorySection"', "sql_saved.js", reverse("sql_saved", args=[self.server.pk])):
            self.assertContains(page, needle)
        saved = (BASE_DIR / "static" / "sql_saved.js").read_text(encoding="utf-8")
        for needle in ("pgdasHistoryAdd", "My recent statements on this server (", "Saved as", "/delete/"):
            self.assertIn(needle, saved)
        self.assertIn("pgdasHistoryAdd", (BASE_DIR / "static" / "sql_run.js").read_text(encoding="utf-8"))
        helper = (BASE_DIR / "static" / "sql_helper.js").read_text(encoding="utf-8")
        for needle in ("function aliasMap", "NOT_ALIASES", "function lookupTable"):
            self.assertIn(needle, helper)
        for script in (saved, helper):
            self.assertNotIn("innerHTML", script)


class PartitionUndoTests(ScriptFixture):
    def setUp(self):
        super().setUp()
        with admin_connect(DB) as conn:
            conn.execute("CREATE TABLE sales (id bigint, saledate date NOT NULL) PARTITION BY RANGE (saledate);"
                         "CREATE TABLE sales_2024 PARTITION OF sales FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');"
                         "CREATE TABLE old (id bigint, saledate date NOT NULL)")

    def parts(self):
        return sorted(r[0] for r in self.query("SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = 'sales'::regclass"))

    def undo(self, entry):
        return self.client.post(reverse("audit_undo", args=[entry.pk]), follow=True)

    def test_an_attach_is_undone_by_a_detach(self):
        _, done = self.go('ALTER TABLE "public"."sales" ATTACH PARTITION "public"."old" FOR VALUES FROM (\'2023-01-01\') TO (\'2024-01-01\')')
        self.assertEqual(done.status_code, 200, done.content)
        entry = AuditEntry.objects.filter(action="alter").latest("pk")
        self.assertEqual(entry.payload["partition_undo"]["statement"], 'ALTER TABLE "public"."sales" DETACH PARTITION "public"."old"')
        self.assertTrue(entry.can_undo)
        self.assertIn("old", self.parts())
        self.undo(entry)
        self.assertEqual(self.parts(), ["sales_2024"])
        entry.refresh_from_db()
        self.assertFalse(entry.can_undo)
        self.assertIsNotNone(entry.restored_at)
        self.undo(entry)                                                                   # once only
        self.assertEqual(AuditEntry.objects.filter(action="update.undo", ok=True).count(), 1)

    def test_a_detach_is_undone_by_an_attach_with_the_old_bound(self):
        _, done = self.go('ALTER TABLE "public"."sales" DETACH PARTITION "public"."sales_2024"', confirm="ALTER")
        self.assertEqual(done.status_code, 200, done.content)
        self.assertEqual(self.parts(), [])
        entry = AuditEntry.objects.filter(action="alter").latest("pk")
        self.assertIn("FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')", entry.payload["partition_undo"]["statement"])
        self.undo(entry)
        self.assertEqual(self.parts(), ["sales_2024"])

    def test_the_undo_page_offers_the_button_and_says_what_it_does(self):
        self.go('ALTER TABLE "public"."sales" ATTACH PARTITION "public"."old" FOR VALUES FROM (\'2023-01-01\') TO (\'2024-01-01\')')
        html = self.client.get(reverse("audit_list")).content.decode()
        self.assertIn('data-verb="partition"', html)
        self.assertIn("Undo this attach", html)

    def test_other_changes_have_no_such_undo(self):
        self.go('ALTER TABLE "public"."old" ADD COLUMN "note" text')
        entry = AuditEntry.objects.filter(action="alter").latest("pk")
        self.assertNotIn("partition_undo", entry.payload)
        self.assertFalse(entry.can_undo)

    def test_an_undo_that_postgresql_refuses_changes_nothing(self):
        self.go('ALTER TABLE "public"."sales" ATTACH PARTITION "public"."old" FOR VALUES FROM (\'2023-01-01\') TO (\'2024-01-01\')')
        entry = AuditEntry.objects.filter(action="alter").latest("pk")
        with admin_connect(DB) as conn:
            conn.execute("DROP TABLE sales CASCADE")                                          # nothing left to detach from
        self.undo(entry)
        entry.refresh_from_db()
        self.assertIsNone(entry.restored_at)
        self.assertTrue(AuditEntry.objects.filter(action="update.undo", ok=False).exists())
