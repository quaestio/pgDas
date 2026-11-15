"""The Dashboards page and its data endpoint: who sees what, caching, and that the sampler's output reaches the charts."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from access_control.models import Installation, Membership, Organization, OrganizationServer
from explorer import tests as shared
from servers.models import ServerConnection

from .models import MetricSample, ServerHealth


class DashboardViewTests(TestCase):
    def setUp(self):
        cache.clear()
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Org")
        self.admin = get_user_model().objects.create_user("a", email="a@o.test", password="pw-12345-abc", is_staff=True, is_superuser=True)
        Membership.objects.create(user=self.admin, organization=self.org, role="super_user")
        self.server = ServerConnection.objects.create(name="Mine", host="h", username="u")
        OrganizationServer.objects.create(server=self.server, organization=self.org)
        ServerHealth.objects.create(server=self.server, version_num=180000, version_text="PostgreSQL 18.0", sample_count=2)
        now = timezone.now()
        for i, minutes in enumerate((10, 5, 0)):
            MetricSample.objects.create(server=self.server, database="", taken_at=now - timedelta(minutes=minutes),
                                        data={"xact_commit": 300 * i, "xact_rollback": 0, "conn_total": 5, "conn_max": 100, "conn_active": 2, "size_total": 1000 + i})
            MetricSample.objects.create(server=self.server, database="appdb", taken_at=now - timedelta(minutes=minutes), data={"xact_commit": 100 * i, "size": 500, "xid_age": 1000})
        self.client = Client()
        self.client.force_login(self.admin)

    def data(self, client=None, **params):
        return (client or self.client).get(reverse("dashboards_data"), {"server": self.server.pk, **params})

    def test_the_page_lists_my_servers_and_the_periods_and_is_in_the_menu(self):
        page = self.client.get(reverse("dashboards"))
        self.assertContains(page, "Mine")
        for needle in ("dashboards.js", "dashboards.css", 'value="6h"', 'value="14d"', "Sample now", reverse("dashboards")):
            self.assertContains(page, needle)

    def test_the_data_has_tiles_panels_tables_and_the_databases_to_pick_from(self):
        payload = self.data().json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["server"]["name"], "Mine")
        self.assertIn("appdb", payload["databases"])
        self.assertEqual(payload["tiles"]["connections"], 5)
        self.assertTrue(payload["panels"]["tps"]["series"])
        self.assertIn("databases", payload["tables"])
        self.assertEqual(payload["range_key"], "6h")

    def test_a_database_and_a_period_can_be_chosen(self):
        payload = self.data(range="1h", db="appdb").json()
        self.assertEqual((payload["range_key"], payload["database"]), ("1h", "appdb"))
        self.assertEqual(self.data(range="bogus").json()["range_key"], "6h")

    def test_only_super_users_see_it(self):
        person = get_user_model().objects.create_user("p", email="p@o.test", password="pw-12345-abc")
        Membership.objects.create(user=person, organization=self.org, role="editor")
        other = Client()
        other.force_login(person)
        self.assertEqual(other.get(reverse("dashboards")).status_code, 403)
        self.assertEqual(self.data(other).status_code, 403)

    def test_another_organizations_server_is_not_there(self):
        stranger = Organization.objects.create(name="Elsewhere")
        foreign = ServerConnection.objects.create(name="Theirs", host="h", username="u")
        OrganizationServer.objects.create(server=foreign, organization=stranger)
        self.assertEqual(self.client.get(reverse("dashboards_data"), {"server": foreign.pk}).status_code, 404)
        self.assertNotContains(self.client.get(reverse("dashboards")), "Theirs")

    def test_the_answer_is_cached_for_a_short_time_and_a_manual_sample_refreshes_it(self):
        from unittest import mock
        self.data()
        with mock.patch("watch.views.series.dashboard", side_effect=AssertionError("computed again")):
            self.assertTrue(self.data().json()["ok"])                                   # served from the cache
        with mock.patch("watch.views.sampling.sample", return_value=3):
            self.client.post(reverse("watch_sample"), {"server": self.server.pk})
        with mock.patch("watch.views.series.dashboard", return_value={"panels": {}, "tiles": {}, "tables": {}, "range": {}}) as recomputed:
            self.data()
        recomputed.assert_called_once()

    def test_a_server_that_has_never_been_sampled_still_gets_a_page(self):
        fresh = ServerConnection.objects.create(name="Fresh", host="h", username="u")
        OrganizationServer.objects.create(server=fresh, organization=self.org)
        payload = self.client.get(reverse("dashboards_data"), {"server": fresh.pk}).json()
        self.assertEqual(payload["range"]["samples"], 0)
        self.assertIn("No samples yet", payload["panels"]["tps"]["note"])

    def test_the_chart_script_is_valid_javascript(self):
        import shutil, subprocess
        from pathlib import Path
        from django.conf import settings
        if not shutil.which("node"):
            self.skipTest("needs Node.js")
        path = Path(settings.BASE_DIR) / "static" / "dashboards.js"
        done = subprocess.run(["node", "-e", f"new Function(require('fs').readFileSync({str(path)!r}.replace(/\\\\/g, '/'), 'utf8')); console.log('ok')"], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.stdout.strip(), "ok", done.stderr)


@override_settings(SERVER_NETWORKS="any", ALLOWED_SERVER_HOSTS=[])
class LiveReadingTests(shared.PgFixture, TestCase):
    def setUp(self):
        cache.clear()
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Org")
        self.admin = get_user_model().objects.create_user("a", email="a@o.test", password="pw-12345-abc", is_staff=True, is_superuser=True)
        Membership.objects.create(user=self.admin, organization=self.org, role="super_user")
        self.server = shared.make_server()
        OrganizationServer.objects.filter(server=self.server).delete()
        OrganizationServer.objects.create(server=self.server, organization=self.org)
        self.client = Client()
        self.client.force_login(self.admin)

    def reading(self, client=None):
        return (client or self.client).get(reverse("dashboards_live"), {"server": self.server.pk})

    def test_a_reading_has_the_gauges_and_the_counters_the_page_needs(self):
        data = self.reading().json()
        self.assertTrue(data["ok"])
        reading = data["reading"]
        for key in ("t", "conn_total", "conn_max", "conn_active", "conn_idle_tx", "blocked", "oldest_xact_s", "longest_query_s", "xact_commit", "xact_rollback",
                    "blks_hit", "blks_read", "tup_written", "deadlocks"):
            self.assertIn(key, reading, key)
        self.assertGreaterEqual(reading["conn_max"], 1)

    def test_the_counters_move_between_two_readings(self):
        first = self.reading().json()["reading"]
        cache.clear()
        with shared.admin_connect(shared.TARGET["NAME"]) as conn:
            conn.execute("SELECT count(*) FROM pgdas_demo.people")
        second = self.reading().json()["reading"]
        self.assertGreater(second["t"], first["t"])
        self.assertGreaterEqual(second["xact_commit"], first["xact_commit"])

    def test_readings_are_cached_for_a_few_seconds_per_server(self):
        from unittest import mock
        first = self.reading().json()["reading"]
        with mock.patch("watch.views.metrics.live", side_effect=AssertionError("read the server again")):
            self.assertEqual(self.reading().json()["reading"]["t"], first["t"])

    def test_an_unreachable_server_is_a_message_not_an_error_page(self):
        from unittest import mock
        from explorer import pg
        cache.clear()
        with mock.patch("watch.views.metrics.live", side_effect=pg.PgError("connection refused")):
            response = self.reading()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"ok": False, "error": "connection refused"})

    def test_only_super_users_and_only_their_own_servers(self):
        person = get_user_model().objects.create_user("p", email="p@o.test", password="pw-12345-abc")
        Membership.objects.create(user=person, organization=self.org, role="editor")
        other = Client()
        other.force_login(person)
        self.assertEqual(self.reading(other).status_code, 403)
        foreign = ServerConnection.objects.create(name="Theirs", host="h", username="u")
        OrganizationServer.objects.create(server=foreign, organization=Organization.objects.create(name="Elsewhere"))
        self.assertEqual(self.client.get(reverse("dashboards_live"), {"server": foreign.pk}).status_code, 404)

    def test_the_page_has_the_live_strip_and_the_refresh_choices(self):
        page = self.client.get(reverse("dashboards"))
        for needle in ("dashLiveTiles", "dashRefresh", 'value="10"', 'value="30" selected', "data-live-url"):
            self.assertContains(page, needle)