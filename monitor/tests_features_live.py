"""The monitoring settings against a real PostgreSQL: reading them, and installing / removing the extension in the scratch database.
ALTER SYSTEM (the server-wide settings) is deliberately not run here: it would change the test server's own configuration."""
from django.contrib.auth import get_user_model
from django.test import Client
from pgdas.testing import TestCase
from django.urls import reverse
from psycopg import sql

from audit.models import AuditEntry
from explorer.tests import admin_connect
from explorer.tests_writes import WriteFixture

from . import features, queries


class LiveFeatureTests(WriteFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.drop_extension)

    def drop_extension(self):
        with admin_connect(self.db) as conn:
            conn.execute("DROP EXTENSION IF EXISTS pg_stat_statements")

    def test_status_reads_the_real_settings(self):
        status = features.status(self.server)
        self.assertEqual(status["database"], self.db)
        self.assertFalse(status["extension"])
        names = {f["name"]: f for f in status["flags"]}
        self.assertEqual(set(names), set(features.FLAGS))
        self.assertEqual(names["track_activities"]["value"], "on")
        self.assertTrue(names["track_activities"]["available"])
        self.assertIn(status["query_id"], ("auto", "on", "off", "regress"))

    def test_the_extension_can_be_installed_and_removed_in_this_database_only(self):
        if not features.status(self.server)["available"]:
            self.skipTest("pg_stat_statements is not installed on this PostgreSQL server")
        features.change(self.server, "extension", True, self.staff)
        self.assertTrue(features.status(self.server)["extension"])
        statements = queries.snapshot(self.server)
        # Without the library preloaded PostgreSQL refuses to read it: the monitor shows its reason instead of failing.
        self.assertTrue(statements["statements"] is not None or statements["statements_note"])
        self.assertEqual(len(AuditEntry.objects.filter(action="monitor.setting", ok=True)), 1)
        features.change(self.server, "extension", False, self.staff)
        self.assertFalse(features.status(self.server)["extension"])

    def test_dotted_setting_names_are_quoted_as_one_name(self):
        """ALTER SYSTEM SET "pg_stat_statements.track" uses the same quoting as this session-level SET, which PostgreSQL accepts."""
        with admin_connect(self.db) as conn:
            conn.execute(sql.SQL("SET {} = 'on'").format(sql.Identifier("pgdas_test.monitor_flag")))
            self.assertEqual(conn.execute("SHOW \"pgdas_test.monitor_flag\"").fetchone()[0], "on")

    def test_a_refused_change_is_answered_and_audited(self):
        client = Client()
        client.force_login(self.staff)
        url = reverse("monitor_feature_change")
        response = client.post(url, {"server": self.server.pk, "name": "fsync", "enabled": "off", "confirm": "APPLY"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown monitoring setting", response.json()["error"])
        self.assertTrue(AuditEntry.objects.filter(action="monitor.setting.failed", ok=False).exists())
        self.assertEqual(client.get(reverse("monitor_features"), {"server": self.server.pk}).json()["database"], self.db)
        plain = Client()
        plain.force_login(get_user_model().objects.create_user("plain", password="pw-12345-abc"))
        self.assertEqual(plain.get(reverse("monitor_features"), {"server": self.server.pk}).status_code, 403)
