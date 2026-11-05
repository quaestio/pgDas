"""The home page's dashboard: totals and per-server facts read live, the buttons of each server row, and what only staff see."""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client
from pgdas.testing import TestCase
from django.urls import reverse

from audit.models import record
from explorer.tests import TARGET
from explorer.tests_writes import WriteFixture
from servers.models import ServerConnection


class DashboardTests(WriteFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.staff)

    def get(self, client=None, **params):
        return (client or self.client).get(reverse("dashboard_data"), params).json()

    def test_totals_and_the_server_row_come_from_the_server_itself(self):
        d = self.get()
        self.assertTrue(d["ok"])
        self.assertEqual((d["totals"]["servers"], d["totals"]["online"], d["totals"]["offline"]), (1, 1, 0))
        self.assertGreaterEqual(d["totals"]["databases"], 1)
        server = d["servers"][0]
        self.assertTrue(server["ok"])
        self.assertTrue(server["version"].startswith("PostgreSQL"))
        self.assertGreater(server["max"], 0)
        self.assertEqual(server["host"], f"{TARGET['HOST']}:{TARGET['PORT']}")
        self.assertIn(self.db, [s["database"] for s in d["storage"]])

    def test_each_server_row_has_the_addresses_of_its_buttons(self):
        pk = self.server.pk
        self.assertEqual(self.get()["servers"][0]["urls"], {
            "sql": f"/c/{pk}/sql/", "test": f"/servers/{pk}/test/", "edit": f"/servers/{pk}/edit/", "remove": f"/servers/{pk}/delete/"})
        for name in ("sql_runner", "server_test", "server_edit", "server_delete"):
            self.assertEqual(self.client.get(reverse(name, args=[pk])).status_code in (200, 302, 405), True, name)

    def test_staff_may_edit_and_remove_others_may_not(self):
        self.assertTrue(self.get()["can_manage"])
        plain = Client()
        plain.force_login(get_user_model().objects.create_user("plain", password="pw-12345-abc"))
        refused = plain.get(reverse("dashboard_data"))                      # the server-wide dashboard is for Super Users only
        self.assertEqual(refused.status_code, 403)
        self.assertFalse(refused.json()["ok"])

    def test_the_size_trend_is_gone(self):
        d = self.get()
        self.assertNotIn("trend", d)
        self.assertNotIn("days", d)

    def test_the_gauges_are_percentages(self):
        gauges = self.get()["gauges"]
        self.assertTrue(0 <= gauges["connections"]["percent"] <= 100)
        self.assertTrue(0 <= gauges["cache_hit"]["percent"] <= 100)

    def test_an_unreachable_server_is_listed_as_offline_with_the_reason(self):
        bad = ServerConnection(name="bad", host=TARGET["HOST"], port=1, database="postgres", username="x", sslmode="prefer")
        bad.set_password("x")
        bad.save()
        d = self.get()
        self.assertEqual((d["totals"]["servers"], d["totals"]["online"], d["totals"]["offline"]), (2, 1, 1))
        row = next(s for s in d["servers"] if s["name"] == "bad")
        self.assertFalse(row["ok"])
        self.assertTrue(row["error"])
        self.assertTrue(row["urls"]["edit"])                              # an unreachable server can still be edited or removed
        self.assertTrue(any("cannot be reached" in a["title"] and a["title"].startswith("bad:") for a in d["attention"]))
        self.assertEqual(d["totals"]["critical"], 1)

    def test_staff_see_activity_and_failed_changes_others_do_not(self):
        record(self.staff, "delete", self.server, "DELETE FROM t WHERE x = 1", ok=False)
        d = self.get()
        self.assertEqual(d["activity"][0]["action"], "delete")
        self.assertFalse(d["activity"][0]["ok"])
        self.assertTrue(any(a["title"].startswith("Failed delete by zed") for a in d["attention"]))
        plain = Client()
        plain.force_login(get_user_model().objects.create_user("plain", password="pw-12345-abc"))
        refused = plain.get(reverse("dashboard_data"))                      # no activity, no failed changes, no totals: nothing at all
        self.assertEqual(refused.status_code, 403)
        self.assertNotIn("activity", refused.json())


class HomePageTests(WriteFixture, TestCase):
    """The dashboard is the home page (the Servers node), not a page of its own."""

    HOME = "/?type=servers&system=1"

    def test_signing_in_lands_on_the_servers_page(self):
        self.assertEqual(settings.LOGIN_REDIRECT_URL, self.HOME)
        user = get_user_model().objects.create_user("signin", email="signin@example.test", password="pw-12345-abc")     # email is the sign-in ID
        response = Client().post(reverse("login"), {"username": "signin@example.test", "password": "pw-12345-abc"})
        self.assertRedirects(response, self.HOME, fetch_redirect_response=False)

    def test_there_is_no_separate_dashboard_page_or_menu_item(self):
        client = Client()
        client.force_login(self.staff)
        self.assertEqual(client.get("/dashboard/").status_code, 404)
        html = client.get("/").content.decode()
        self.assertNotIn('href="/dashboard/"', html)
        self.assertNotIn("▦</span> Dashboard", html)
        self.assertEqual(Client().get(reverse("dashboard_data")).status_code, 302)       # the data still needs a login

    def test_the_home_page_has_the_dashboard_panels_and_no_size_chart(self):
        client = Client()
        client.force_login(self.staff)
        html = client.get("/", {"type": "servers", "system": "1"}).content.decode()
        block = html[html.index('id="servers-overview"'):html.index('id="explorer"')]
        for text in ("Command Overview", "SERVER STATUS", "RESOURCE USAGE", "LARGEST DATABASES", "RECENT ACTIVITY", "NEEDS ATTENTION",
                     'id="mServers"', 'id="mDatabases"', 'id="mUsers"', 'id="mAlerts"', "Add server"):
            self.assertIn(text, block)
        self.assertNotIn("TOTAL SIZE", html)
        self.assertNotIn('id="dTrend"', html)
        self.assertIn("static/dashboard.js", html)
        self.assertIn('data-url="/dashboard/data/"', block)
        self.assertIn(" hidden", html[html.index('id="detail"'):html.index('id="detail"') + 120])       # the old server table pane stays closed here
        self.assertIn("csrfmiddlewaretoken", block)                                       # the Test button posts a form
