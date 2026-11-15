"""The statistics collector and the load-aware sampler (PostgreSQL 12-18 query selection, due times, back-off, lease, switches)."""
from datetime import timedelta
from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from pgdas.testing import TestCase

from access_control.models import Organization, OrganizationServer
from explorer import pg
from explorer import tests as shared
from servers.models import ServerConnection

from . import metrics, sampling
from .models import MetricSample, ServerHealth, SizeSample, WatchRun


class VersionChoiceTests(SimpleTestCase):
    def test_the_database_statistics_follow_the_version(self):
        sql12, columns12 = metrics.database_sql(120000)
        sql14, columns14 = metrics.database_sql(140000)
        self.assertNotIn("sessions", columns12)
        self.assertIn("active_time", columns14)
        self.assertIn("xact_commit", columns12)
        self.assertNotIn("pg_database_size", sql12)                                    # sizes are a separate, slower group

    def test_the_checkpointer_moved_in_17(self):
        old = metrics.checkpoint_sql(160000)
        new = metrics.checkpoint_sql(170000)
        self.assertEqual(len(old), 1)
        self.assertIn("pg_stat_bgwriter", old[0][0])
        self.assertIn("checkpoints_req", old[0][1])
        self.assertEqual(len(new), 2)
        self.assertIn("pg_stat_checkpointer", new[0][0])
        self.assertIn("checkpoints_req", new[0][1])                                   # the same keys, so charts do not care

    def test_the_statement_time_column_was_renamed_in_13(self):
        self.assertIn("total_time", metrics.statements_sql(120000, "public"))
        self.assertNotIn("total_exec_time", metrics.statements_sql(120000, "public"))
        self.assertIn("total_exec_time", metrics.statements_sql(150000, "public"))


@override_settings(SERVER_NETWORKS="any", ALLOWED_SERVER_HOSTS=[])
class CollectorTests(shared.PgFixture, TestCase):
    def test_one_sample_has_the_server_the_databases_and_nothing_skipped(self):
        server = shared.make_server()
        data = metrics.collect(server)
        self.assertGreaterEqual(data["meta"]["version"], 120000)
        self.assertEqual(data["meta"]["skipped"], [])
        for key in ("conn_total", "conn_max", "xact_commit", "blks_hit", "tup_inserted", "deadlocks", "temp_bytes", "wal_lsn", "checkpoints_timed",
                    "size_total", "locks", "standbys", "slots"):
            self.assertIn(key, data["server"], key)
        self.assertIn(shared.TARGET["NAME"], data["databases"])
        db = data["databases"][shared.TARGET["NAME"]]
        self.assertTrue({"size", "xid_age", "xact_commit", "numbackends"} <= set(db))
        self.assertGreater(db["size"], 0)

    def test_a_group_the_login_may_not_read_is_skipped_not_fatal(self):
        server = shared.make_server()
        real = metrics.checkpoint_sql
        with mock.patch("watch.metrics.checkpoint_sql", lambda version: [("SELECT nothing FROM no_such_view", ["x"])]):
            data = metrics.collect(server)
        self.assertTrue(any(note.startswith("checkpoints") for note in data["meta"]["skipped"]))
        self.assertIn("xact_commit", data["server"])                                  # the rest of the sample is intact

    def test_the_sample_can_be_stored_as_json(self):
        import json
        json.dumps(metrics.collect(shared.make_server()), default=str)


@override_settings(SERVER_NETWORKS="any", ALLOWED_SERVER_HOSTS=[], WATCH_INTERVAL_SECONDS=300, WATCH_MAX_BACKOFF_SECONDS=3600)
class SamplerTests(shared.PgFixture, TestCase):
    def setUp(self):
        self.server = shared.make_server()

    def test_a_run_samples_a_due_server_and_stores_rows_health_and_size(self):
        results = sampling.run_due()
        self.assertEqual([name for name, _ in results], ["test"])
        self.assertGreater(MetricSample.objects.filter(server=self.server, database="").count(), 0)
        self.assertGreater(MetricSample.objects.filter(server=self.server).exclude(database="").count(), 0)
        self.assertGreater(SizeSample.objects.filter(server=self.server).count(), 0)
        health = ServerHealth.objects.get(server=self.server)
        self.assertEqual((health.failures, health.last_error), (0, ""))
        self.assertGreaterEqual(health.version_num, 120000)
        self.assertGreater(health.next_due, timezone.now())
        run = WatchRun.objects.get()
        self.assertEqual((run.sampled, run.failed, run.due), (1, 0, 1))
        self.assertIsNotNone(run.finished_at)

    def test_a_server_is_not_sampled_again_until_it_is_due(self):
        sampling.run_due()
        before = MetricSample.objects.count()
        self.assertEqual(sampling.run_due(), [])
        self.assertEqual(MetricSample.objects.count(), before)
        later = timezone.now() + timedelta(seconds=400)
        self.assertEqual(len(sampling.run_due(later)), 1)
        self.assertGreater(MetricSample.objects.count(), before)

    def test_force_samples_regardless(self):
        sampling.run_due()
        self.assertEqual(len(sampling.run_due(force=True)), 1)

    def test_a_dead_server_is_tried_less_and_less_often(self):
        dead = ServerConnection.objects.create(name="dead", host="127.0.0.1", port=1, username="u")
        now = timezone.now()
        waits = []
        for _ in range(8):
            sampling.run_due(now + timedelta(days=len(waits)), force=False)
            health = ServerHealth.objects.get(server=dead)
            waits.append((health.next_due - health.last_attempt_at).total_seconds())
        self.assertEqual(ServerHealth.objects.get(server=dead).failures, 8)
        self.assertEqual(waits[0], 600)
        self.assertEqual(waits[1], 1200)
        self.assertEqual(max(waits), 3600)                                              # capped
        self.assertTrue(ServerHealth.objects.get(server=dead).last_error)

    def test_one_dead_server_does_not_stop_the_others(self):
        ServerConnection.objects.create(name="dead", host="127.0.0.1", port=1, username="u")
        outcomes = dict(sampling.run_due(force=True))
        self.assertIsInstance(outcomes["test"], int)
        self.assertTrue(outcomes["dead"].startswith("failed"))
        run = WatchRun.objects.get()
        self.assertEqual((run.sampled, run.failed), (1, 1))

    def test_two_runs_cannot_overlap(self):
        WatchRun.objects.create(started_at=timezone.now())                              # one that has not finished
        self.assertIsNone(sampling.run_due())
        WatchRun.objects.update(started_at=timezone.now() - timedelta(hours=1))         # ... but not forever
        self.assertIsNotNone(sampling.run_due())

    def test_an_organization_can_be_switched_off(self):
        org = Organization.objects.create(name="Quiet")
        OrganizationServer.objects.filter(server=self.server).delete()
        OrganizationServer.objects.create(server=self.server, organization=org)
        Organization.objects.filter(pk=org.pk).update(monitoring_enabled=False)
        self.assertEqual(sampling.run_due(force=True), [])
        Organization.objects.filter(pk=org.pk).update(monitoring_enabled=True)
        self.assertEqual(len(sampling.run_due(force=True)), 1)

    def test_a_locked_server_is_skipped_with_the_reason(self):
        locked = ServerConnection.objects.create(name="locked", host="127.0.0.1", port=1, username="u", save_password=False)
        outcomes = dict(sampling.run_due(force=True))
        self.assertIn("locked", outcomes["locked"])
        self.assertFalse(ServerHealth.objects.filter(server=locked).exists())          # nothing was attempted, so no back-off either

    def test_old_samples_are_pruned_in_batches(self):
        sampling.run_due()
        old = timezone.now() - timedelta(days=400)
        MetricSample.objects.filter(server=self.server).update(taken_at=old)
        SizeSample.objects.filter(server=self.server).update(taken_at=old)
        removed = sampling.prune()
        self.assertGreater(removed, 0)
        self.assertFalse(MetricSample.objects.exists())

    def test_the_statement_list_is_kept_with_every_third_sample_only(self):
        statements = [{"id": "1", "query": "select 1", "calls": 1, "total": 1.0, "rows": 1, "hit": 1, "read": 0}]
        data = {"server": {"xact_commit": 1, "statements": statements}, "databases": {}, "meta": {"version": 160000, "version_text": "PostgreSQL 16", "in_recovery": False,
                "started_at": timezone.now().isoformat(), "skipped": [], "settings": {}}}
        kept = []
        for number in range(6):
            sampling.store(self.server, data, timezone.now() + timedelta(minutes=number))
        for sample in MetricSample.objects.filter(server=self.server, database="").order_by("taken_at"):
            kept.append("statements" in sample.data)
        self.assertEqual(kept, [True, False, False, True, False, False])

    def test_the_command_runs_once_and_reports(self):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command("run_watch", "--no-notify", stdout=out)
        self.assertIn("test:", out.getvalue())
        out = StringIO()
        call_command("run_watch", "--no-notify", stdout=out)                            # nothing due now
        self.assertNotIn("test:", out.getvalue())