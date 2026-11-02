"""The dashboard: totals and per-server facts read live, the size trend, and what only staff see."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from audit.models import record
from explorer.tests import TARGET
from explorer.tests_writes import WriteFixture
from servers.models import ServerConnection
from watch.models import SizeSample

from . import data


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
        d = self.get(plain)
        self.assertFalse(d["is_staff"])
        self.assertEqual((d["activity"], d["attention"]), ([], []))
        self.assertEqual(d["totals"]["servers"], 1)                      # the rest is shown to everyone who signs in

    def test_the_trend_adds_up_the_latest_size_of_every_server_per_hour(self):
        other = ServerConnection(name="other", host=TARGET["HOST"], port=int(TARGET["PORT"]), database="postgres", username=TARGET["USER"], sslmode="prefer")
        other.set_password(TARGET["PASSWORD"])
        other.save()
        now = timezone.now().replace(minute=30, second=0, microsecond=0)
        for server, hours_ago, sizes in ((self.server, 3, (100, 50)), (self.server, 1, (120, 60)), (other, 2, (10,)), (other, 0, (14,))):
            for i, size in enumerate(sizes):
                SizeSample.objects.create(server=server, database=f"d{i}", size=size, taken_at=now - timedelta(hours=hours_ago))
        points = data._trend(1)
        sizes = [p["size"] for p in points]
        self.assertNotIn(150, sizes)                                     # no point before `other` has a size: the line must not climb as servers appear
        self.assertEqual(sizes, [160, 190, 194])
        self.assertEqual(self.get(days=1)["days"], 1)
        self.assertEqual(self.get(days=99)["days"], 7)

    def test_pages_need_a_login_and_signing_in_lands_on_the_dashboard(self):
        anonymous = Client()
        self.assertEqual(anonymous.get(reverse("dashboard")).status_code, 302)
        self.assertEqual(anonymous.get(reverse("dashboard_data")).status_code, 302)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        user = get_user_model().objects.create_user("signin", password="pw-12345-abc")
        response = anonymous.post(reverse("login"), {"username": user.username, "password": "pw-12345-abc"})
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse("dashboard")), 'href="/dashboard/"')
