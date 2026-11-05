"""Size samples and trends, and the alerts of the Alerts & trends page."""
from datetime import timedelta
from io import StringIO

import psycopg
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, override_settings
from pgdas.testing import TestCase
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEntry
from backups.models import Backup, BackupSchedule
from explorer.tests import TARGET
from explorer.tests_writes import WriteFixture
from servers.models import ServerConnection

from . import alerts, sampling
from .models import SizeSample


def titles(found):
    return [a["title"] for a in found]


class SamplingTests(WriteFixture, TestCase):
    def test_a_sample_has_every_visible_database(self):
        count = sampling.sample(self.server)
        self.assertGreaterEqual(count, 1)
        self.assertIn(self.db, SizeSample.objects.filter(server=self.server).values_list("database", flat=True))
        self.assertTrue(all(s.size > 0 for s in SizeSample.objects.all()))

    def test_old_samples_are_pruned_and_new_ones_kept(self):
        now = timezone.now()
        SizeSample.objects.create(server=self.server, database="old", size=1, taken_at=now - timedelta(days=200))
        SizeSample.objects.create(server=self.server, database="new", size=1, taken_at=now - timedelta(days=2))
        sampling.prune(now)
        self.assertEqual(list(SizeSample.objects.values_list("database", flat=True)), ["new"])

    def test_the_series_gives_size_change_and_points_for_the_period(self):
        now = timezone.now()
        for days, size in ((20, 100), (5, 150), (1, 200)):
            SizeSample.objects.create(server=self.server, database="shop", size=size * 1024 * 1024, taken_at=now - timedelta(days=days))
        week = sampling.series(self.server, 7)
        self.assertEqual((week[0]["database"], week[0]["samples"], week[0]["percent"]), ("shop", 2, 33.3))
        self.assertEqual(week[0]["change_text"], "+50.0 MB")
        month = sampling.series(self.server, 30)
        self.assertEqual((month[0]["samples"], month[0]["percent"]), (3, 100.0))

    def test_an_unreachable_server_is_reported_not_fatal(self):
        bad = ServerConnection(name="bad", host=TARGET["HOST"], port=1, database="postgres", username="x", sslmode="prefer")
        bad.set_password("x")
        bad.save()
        results = dict(sampling.sample_all())
        self.assertTrue(str(results["bad"]).startswith("failed:"))
        self.assertIsInstance(results["test"], int)

    def test_the_command_samples_every_server(self):
        out = StringIO()
        call_command("run_watch", stdout=out)
        self.assertIn("databases sampled", out.getvalue())
        self.assertTrue(SizeSample.objects.exists())


class AlertTests(WriteFixture, TestCase):
    def other(self):
        conn = psycopg.connect(host=TARGET["HOST"], port=TARGET["PORT"], dbname=TARGET["NAME"], user=TARGET["USER"],
                               password=TARGET["PASSWORD"], connect_timeout=5)
        self.addCleanup(conn.close)
        return conn

    def test_a_quiet_server_has_no_live_alerts(self):
        with override_settings(WATCH_CONNECTIONS_PERCENT=100):
            self.assertEqual([a for a in alerts.live(self.server) if "idle in a transaction" in a["title"]], [])

    def test_an_idle_in_transaction_session_raises_a_warning(self):
        conn = self.other()
        conn.execute("UPDATE pgdas_w.logged SET what = 'held' WHERE id = 1")
        with override_settings(WATCH_IDLE_TX_SECONDS=0):
            found = alerts.live(self.server)
        self.assertTrue(any("idle in a transaction too long" in t for t in titles(found)))
        self.assertEqual(found[0]["level"], "warning")

    def test_connections_over_the_share_raise_an_alert(self):
        with override_settings(WATCH_CONNECTIONS_PERCENT=0):
            self.assertTrue(any("connections are in use" in t for t in titles(alerts.live(self.server))))

    def test_an_unreachable_server_is_one_critical_alert(self):
        bad = ServerConnection(name="bad", host=TARGET["HOST"], port=1, database="postgres", username="x", sslmode="prefer")
        bad.set_password("x")
        bad.save()
        found = alerts.evaluate(bad)
        self.assertEqual([(a["level"], a["title"]) for a in found], [("critical", "The server cannot be reached")])

    def test_fast_growth_needs_history_a_percentage_and_a_worthwhile_amount(self):
        now = timezone.now()
        mb = 1024 * 1024
        for name, first, last, hours in (("grows", 1000, 1600, 100), ("small", 1, 3, 100), ("fresh", 1000, 3000, 2), ("steady", 1000, 1050, 100)):
            SizeSample.objects.create(server=self.server, database=name, size=first * mb, taken_at=now - timedelta(hours=hours))
            SizeSample.objects.create(server=self.server, database=name, size=last * mb, taken_at=now)
        found = alerts.growth(self.server, now)
        self.assertEqual(len(found), 1)
        self.assertIn("grows grew by 60%", found[0]["title"])
        self.assertIn("1000.0 MB to 1.6 GB", found[0]["detail"])

    def test_failed_and_overdue_backups_are_alerts(self):
        now = timezone.now()
        schedule = BackupSchedule.objects.create(server=self.server, database=self.db, every_hours=1, keep=3)
        BackupSchedule.objects.filter(pk=schedule.pk).update(last_run=now - timedelta(hours=5))
        Backup.objects.create(server=self.server, server_name="test", database=self.db, kind="scheduled", status="failed", error="disk full")
        found = alerts.backups(self.server, now)
        self.assertEqual({a["level"] for a in found}, {"critical", "warning"})
        self.assertTrue(any("failed" in t for t in titles(found)))
        self.assertTrue(any("overdue" in t for t in titles(found)))
        self.assertIn("disk full", [a for a in found if a["level"] == "critical"][0]["detail"])

    def test_a_backup_that_is_on_time_raises_nothing(self):
        schedule = BackupSchedule.objects.create(server=self.server, database=self.db, every_hours=24, keep=3)
        BackupSchedule.objects.filter(pk=schedule.pk).update(last_run=timezone.now() - timedelta(hours=3))
        Backup.objects.create(server=self.server, server_name="test", database=self.db, kind="scheduled", status="ok")
        self.assertEqual(alerts.backups(self.server), [])


class PageTests(WriteFixture, TestCase):
    def test_the_data_endpoint_returns_alerts_and_series_for_staff_only(self):
        client = Client()
        client.force_login(self.staff)
        sampling.sample(self.server)
        data = client.get(reverse("watch_data"), {"server": self.server.pk, "days": 30}).json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["days"], 30)
        self.assertIn(self.db, [s["database"] for s in data["series"]])
        self.assertEqual(client.get(reverse("watch_data"), {"server": self.server.pk, "days": "x"}).json()["days"], 7)
        self.assertEqual(client.get(reverse("watch"))["Content-Type"].split(";")[0], "text/html; charset=utf-8".split(";")[0])
        plain = Client()
        plain.force_login(get_user_model().objects.create_user("plain", password="pw-12345-abc"))
        for name in ("watch", "watch_data"):
            self.assertEqual(plain.get(reverse(name), {"server": self.server.pk}).status_code, 403)
        self.assertEqual(plain.post(reverse("watch_sample"), {"server": self.server.pk}).status_code, 403)

    def test_sample_now_takes_a_sample_and_is_audited(self):
        client = Client()
        client.force_login(self.staff)
        self.assertTrue(client.post(reverse("watch_sample"), {"server": self.server.pk}).json()["ok"])
        self.assertTrue(SizeSample.objects.exists())
        self.assertTrue(AuditEntry.objects.filter(action="watch.sample").exists())
