"""The 'My recent statements' list on the SQL page: your own statements on this server, newest first, each one once."""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from audit.models import record

from .tests_writes import WriteFixture


class HistoryTests(WriteFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)
        self.url = reverse("sql_runner", args=[self.server.pk])

    def test_a_run_statement_appears_and_is_not_repeated(self):
        for _ in range(3):
            self.client.post(self.url, {"sql": "SELECT 1 AS one", "db": self.db})
        page = self.client.get(self.url)
        self.assertEqual(len(page.context["history"]), 1)
        self.assertContains(page, "My recent statements on this server (1)")
        self.assertContains(page, 'data-sql="SELECT 1 AS one"')

    def test_newest_first_and_failures_are_marked(self):
        self.client.post(self.url, {"sql": "SELECT 1", "db": self.db})
        self.client.post(self.url, {"sql": "SELECT nope FROM nowhere", "db": self.db})
        history = self.client.get(self.url).context["history"]
        self.assertEqual([(e.detail, e.ok) for e in history], [("SELECT nope FROM nowhere", False), ("SELECT 1", True)])
        self.assertContains(self.client.get(self.url), "failed")

    def test_only_my_own_statements_on_this_server(self):
        other = get_user_model().objects.create_user("pat", password="pw-12345-abc")
        record(other, "sql.run", self.server, "SELECT 'pats secret'")
        record(self.staff, "sql.run", None, "SELECT 'other server'")
        record(self.staff, "server.edit", self.server, "not a statement")
        self.client.post(self.url, {"sql": "SELECT 'mine'", "db": self.db})
        page = self.client.get(self.url)
        self.assertEqual([e.detail for e in page.context["history"]], ["SELECT 'mine'"])
        self.assertNotContains(page, "pats secret")
        pat = Client()
        pat.force_login(other)
        self.assertEqual([e.detail for e in pat.get(self.url).context["history"]], ["SELECT 'pats secret'"])

    def test_changing_statements_are_listed_too_and_the_list_is_capped(self):
        record(self.staff, "update", self.server, "UPDATE t SET a = 1")
        for n in range(30):
            record(self.staff, "sql.run", self.server, f"SELECT {n}")
        history = self.client.get(self.url).context["history"]
        self.assertEqual(len(history), 20)
        self.assertEqual(history[0].detail, "SELECT 29")

    def test_statement_text_is_escaped(self):
        record(self.staff, "sql.run", self.server, 'SELECT "<script>alert(1)</script>"')
        page = self.client.get(self.url)
        self.assertNotContains(page, "<script>alert(1)</script>")

    def test_no_history_no_panel(self):
        self.assertNotContains(self.client.get(self.url), "My recent statements")
