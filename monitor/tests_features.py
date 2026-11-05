from unittest.mock import patch

from django.contrib.auth import get_user_model
from pgdas.testing import TestCase
from django.urls import reverse

from explorer.pg import PgError
from servers.models import ServerConnection
from . import features


class FeatureControlsTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user("settings_staff", is_staff=True)
        self.viewer = get_user_model().objects.create_user("settings_viewer")
        self.server = ServerConnection.objects.create(name="Test", host="localhost", username="test", created_by=self.staff)
        self.url = reverse("monitor_feature_change")
        self.data = {"server": self.server.pk, "name": "track_io_timing", "enabled": "on", "confirm": "APPLY"}

    @patch("monitor.features.change")
    def test_viewer_cannot_change_settings(self, change):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.post(self.url, self.data).status_code, 403)
        change.assert_not_called()

    @patch("monitor.features.change")
    def test_confirmation_and_post_required(self, change):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.data["confirm"] = ""
        self.assertEqual(self.client.post(self.url, self.data).status_code, 400)
        change.assert_not_called()

    @patch("monitor.features.change", side_effect=PgError("permission denied"))
    def test_postgres_refusal_is_visible(self, change):
        self.client.force_login(self.staff)
        response = self.client.post(self.url, self.data)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "permission denied")

    @patch("monitor.features.pg.connect_maintenance")
    def test_arbitrary_settings_rejected_before_connection(self, connect):
        with self.assertRaises(PgError):
            features.change(self.server, "fsync", False, self.staff)
        connect.assert_not_called()

    @patch("monitor.features.pg.connect_maintenance")
    def test_preload_preserves_other_pending_libraries(self, connect):
        conn = connect.return_value.__enter__.return_value
        conn.execute.return_value.fetchone.side_effect = [(1,), ("pg_cron,auto_explain",), (True,)]
        features.change(self.server, "preload", True, self.staff)
        statement = conn.execute.call_args_list[2].args[0].as_string()
        self.assertIn("pg_cron,auto_explain,pg_stat_statements", statement)

    @patch("monitor.features.pg.connect_maintenance")
    def test_preload_is_not_appended_twice(self, connect):
        conn = connect.return_value.__enter__.return_value
        conn.execute.return_value.fetchone.side_effect = [(1,), ("pg_cron,pg_stat_statements",)]
        self.assertIn("already configured", features.change(self.server, "preload", True, self.staff))
        self.assertEqual(conn.execute.call_count, 2)
