"""Chart data from stored samples: counters become rates, restarts and silences become gaps, long ranges are compressed."""
from datetime import timedelta

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from servers.models import ServerConnection

from . import series
from .models import MetricSample, ServerHealth


class RateTests(SimpleTestCase):
    def rows(self, values, step=300, key="x"):
        return [(i * step, {key: v}) for i, v in enumerate(values)]

    def test_a_counter_becomes_a_per_second_rate(self):
        self.assertEqual(series.rate(self.rows([0, 300, 900]), "x", 300), [(300, 1.0), (600, 2.0)])

    def test_a_restart_or_reset_is_a_gap_not_a_huge_negative_or_wrong_number(self):
        points = series.rate(self.rows([1000, 1300, 5, 305]), "x", 300)
        self.assertEqual(points, [(300, 1.0), (900, 1.0)])                           # the interval where it went backwards is left out

    def test_a_long_silence_is_a_gap(self):
        rows = [(0, {"x": 0}), (300, {"x": 300}), (5000, {"x": 9999}), (5300, {"x": 10299})]
        self.assertEqual(series.rate(rows, "x", 300), [(300, 1.0), (5300, 1.0)])

    def test_scale_gives_per_minute(self):
        self.assertEqual(series.rate(self.rows([0, 30]), "x", 300, 60), [(300, 6.0)])

    def test_a_missing_value_is_skipped(self):
        rows = [(0, {"x": 0}), (300, {}), (600, {"x": 600})]
        self.assertEqual(series.rate(rows, "x", 300), [])

    def test_a_ratio_uses_the_changes_not_the_totals(self):
        rows = [(0, {"hit": 0, "read": 0}), (300, {"hit": 90, "read": 10}), (600, {"hit": 90, "read": 110})]
        self.assertEqual(series.ratio(rows, ["hit"], ["hit", "read"], 300), [(300, 90.0), (600, 0.0)])

    def test_a_ratio_with_nothing_to_divide_is_skipped(self):
        rows = [(0, {"hit": 5, "read": 5}), (300, {"hit": 5, "read": 5})]
        self.assertEqual(series.ratio(rows, ["hit"], ["hit", "read"], 300), [])

    def test_statements_give_calls_per_second_and_milliseconds_per_call(self):
        a = [{"id": "1", "calls": 100, "total": 1000.0}, {"id": "2", "calls": 10, "total": 50.0}]
        b = [{"id": "1", "calls": 400, "total": 1600.0}, {"id": "2", "calls": 10, "total": 50.0}, {"id": "3", "calls": 999, "total": 9999.0}]
        calls, millis = series.statements_rate([(0, {"statements": a}), (300, {"statements": b})], 300)
        self.assertEqual(calls, [(300, 1.0)])                                           # only statements seen in both samples count
        self.assertEqual(millis, [(300, 2.0)])

    def test_compress_averages_buckets_and_caps_the_points(self):
        points = [(float(i), float(i)) for i in range(1000)]
        compressed = series.compress(points, 0, 1000)
        self.assertLessEqual(len(compressed), series.MAX_POINTS)
        self.assertAlmostEqual(compressed[0][1], sum(range(0, 5)) / 5, delta=2)
        self.assertEqual(series.compress([], 0, 10), [])
        self.assertEqual(series.compress([(1.1, 1.0), (1.2, 9.0)], 0, 10, "max")[0][1], 9.0)


@override_settings(WATCH_INTERVAL_SECONDS=300)
class DashboardTests(TestCase):
    def setUp(self):
        self.server = ServerConnection.objects.create(name="S", host="h", username="u")
        self.now = timezone.now()
        ServerHealth.objects.create(server=self.server, version_num=170002, version_text="PostgreSQL 17.2", sample_count=13,
                                    started_at=self.now - timedelta(days=3), settings={"shared_buffers": "128MB"}, capabilities={"skipped": [], "pg_stat_statements": True})
        rows = []
        for i in range(13):                                                              # one hour of 5-minute samples
            at = self.now - timedelta(minutes=5 * (12 - i))
            server = {"xact_commit": 300 * i, "xact_rollback": 3 * i, "blks_hit": 900 * i, "blks_read": 100 * i, "tup_returned": 6000 * i, "tup_fetched": 3000 * i,
                      "tup_inserted": 30 * i, "tup_updated": 15 * i, "tup_deleted": 3 * i, "deadlocks": i // 6, "conflicts": 0, "temp_bytes": 1000 * i,
                      "wal_bytes": 3000000 * i, "wal_fpi": 30 * i, "checkpoints_timed": i, "checkpoints_req": i // 4, "buffers_checkpoint": 100 * i,
                      "conn_total": 20 + i, "conn_max": 100, "conn_active": 5, "conn_idle": 10 + i, "conn_idle_tx": 1, "conn_idle_tx_aborted": 0, "blocked": 0,
                      "conn_waiting_lock": 0, "locks_waiting": 0, "oldest_xact_s": 12.5, "longest_query_s": 1.5, "oldest_idle_tx_s": 0, "size_total": 5 * 2 ** 30 + i * 1000,
                      "autovacuum_workers": 1, "prepared_xacts": 0, "wal_dir_bytes": 2 ** 28, "standbys": [{"name": "replica1", "client": "10.0.0.9", "replay_lag": 1000.0 * i, "replay_lag_s": 0.5}],
                      "slots": [{"name": "slot1", "retained": 5000.0 * i, "active": True}]}
            if i % 3 == 0:
                server["statements"] = [{"id": "q1", "query": "select 1", "calls": 1000 * i, "total": 2000.0 * i, "rows": 1000 * i, "hit": 900 * i, "read": 100 * i}]
            rows.append(MetricSample(server=self.server, database="", taken_at=at, data=server))
            rows.append(MetricSample(server=self.server, database="app", taken_at=at, data={"xact_commit": 200 * i, "xact_rollback": 2 * i, "blks_hit": 800 * i,
                                                                                            "blks_read": 200 * i, "size": 2 ** 30 + i, "xid_age": 20_000_000, "conn_total": 10, "conn_active": 2}))
        MetricSample.objects.bulk_create(rows)

    def test_the_numbers_are_the_rates_the_samples_imply(self):
        data = series.dashboard(self.server, 1)
        panels = data["panels"]
        commits = {s["name"]: s for s in panels["tps"]["series"]}["commits"]["points"]
        self.assertAlmostEqual(commits[-1][1], 1.0, places=2)                           # 300 commits per 5 minutes
        self.assertAlmostEqual(data["tiles"]["tps"], 1.01, places=2)                   # commits + rollbacks
        self.assertAlmostEqual(data["tiles"]["cache_hit"], 90.0, places=1)
        self.assertAlmostEqual(data["tiles"]["wal_rate"], 10000.0, places=0)
        self.assertEqual((data["tiles"]["connections"], data["tiles"]["max_connections"]), (32, 100))
        self.assertEqual((data["tiles"]["role"], data["tiles"]["version"]), ("primary", "PostgreSQL 17.2"))
        self.assertEqual(data["tiles"]["standbys"], 1)
        self.assertGreater(data["tiles"]["uptime"], 2 * 86400)
        self.assertLess(data["tiles"]["last_sample_age"], 60)

    def test_a_database_selection_uses_that_databases_counters(self):
        data = series.dashboard(self.server, 1, "app")
        commits = {s["name"]: s for s in data["panels"]["tps"]["series"]}["commits"]["points"]
        self.assertAlmostEqual(commits[-1][1], 200 / 300, places=2)
        self.assertEqual(data["panels"]["size"]["series"][0]["points"][-1][1] > 2 ** 30, True)
        self.assertAlmostEqual(data["tiles"]["cache_hit"], 80.0, places=1)

    def test_statements_replication_and_slots_have_their_panels_and_tables(self):
        data = series.dashboard(self.server, 1)
        self.assertTrue(data["panels"]["qps"]["series"])
        self.assertEqual(data["tables"]["statements"][0]["query"], "select 1")
        self.assertAlmostEqual(data["tables"]["statements"][0]["mean_ms"], 2.0)
        self.assertEqual(data["tables"]["standbys"][0]["name"], "replica1")
        self.assertEqual(data["tables"]["slots"][0]["name"], "slot1")
        self.assertTrue(data["panels"]["lag_bytes"]["series"])
        self.assertTrue(data["panels"]["slots"]["series"])
        self.assertEqual(data["tables"]["databases"][0]["database"], "app")
        self.assertEqual(data["tables"]["settings"]["shared_buffers"], "128MB")

    def test_a_panel_without_data_says_why(self):
        panels = series.dashboard(self.server, 1)["panels"]
        self.assertTrue(panels["io"]["empty"])
        self.assertEqual(panels["io"]["note"], "No data in this period yet.")                  # a 17 has pg_stat_io but nothing was collected
    def test_an_old_server_is_told_what_it_lacks(self):
        ServerHealth.objects.filter(server=self.server).update(version_num=130004, version_text="PostgreSQL 13.4")
        panels = series.dashboard(self.server, 1)["panels"]
        self.assertIn("Needs PostgreSQL 14", panels["states_time"]["note"])
        self.assertIn("Needs PostgreSQL 16", panels["io"]["note"])

    def test_a_server_that_was_never_sampled_gets_an_honest_page(self):
        other = ServerConnection.objects.create(name="New", host="h2", username="u")
        data = series.dashboard(other, 6)
        self.assertEqual(data["range"]["samples"], 0)
        self.assertIn("No samples yet", data["panels"]["tps"]["note"])
        self.assertIsNone(data["tiles"]["connections"])

    def test_every_panel_is_json_ready(self):
        import json
        json.dumps(series.dashboard(self.server, 24))