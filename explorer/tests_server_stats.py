"""The statistics on a server's overview page: uptime, connections now, work done since the counters were reset, and the key settings."""
from django.urls import reverse

from . import pg
from .tests_admin import DB
from .tests_table_script import ScriptFixture


class ServerStatsTests(ScriptFixture):
    def stats(self):
        with pg.connect(self.server, DB) as conn:
            return pg.server_stats(conn)

    def test_the_statistics_have_every_group(self):
        stats = self.stats()
        basics = stats["basics"]
        self.assertIn(basics["role"], ("primary", "standby"))
        self.assertGreaterEqual(basics["databases"], 1)
        self.assertGreaterEqual(basics["uptime"].total_seconds(), 0)
        sessions = stats["sessions"]
        self.assertGreaterEqual(sessions["limit"], 1)
        self.assertIn("used_percent", sessions)
        work = stats["work"]
        self.assertGreaterEqual(work["commits"], 0)
        self.assertTrue(work["cache_hit"] is None or 0 <= work["cache_hit"] <= 100)
        self.assertIn("temp_size_text", work)
        names = [s["name"] for s in stats["settings"]]
        for name in ("shared_buffers", "work_mem", "max_connections", "wal_level"):
            self.assertIn(name, names)

    def test_a_group_that_cannot_be_read_is_left_out_not_fatal(self):
        with pg.connect(self.server, DB) as conn:
            self.assertIsNone(pg._group(conn, lambda: conn.execute("SELECT * FROM table_that_is_not_there")))
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)                  # the connection is still usable afterwards

    def test_the_overview_page_shows_the_groups(self):
        html = self.client.get(reverse("detail"), {"type": "server", "server": self.server.pk}).content.decode()
        for text in ("Connections now", "Work done", "Key settings", "Running since", "Cache hit", "shared_buffers", "Idle in transaction"):
            self.assertIn(text, html)
        self.assertIn(reverse("dashboards") + f"?server={self.server.sid}", html)
