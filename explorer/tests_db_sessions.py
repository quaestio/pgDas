"""Server > Databases: every row shows how many other sessions are connected (also 0), and clicking it opens a popup to check and close them."""
import re
from pathlib import Path

import psycopg
from django.contrib.auth import get_user_model
from django.test import Client
from pgdas.testing import TestCase
from django.urls import reverse

from audit.models import AuditEntry

from . import pg
from .tests import TARGET, admin_connect
from .tests_admin import AdminFixture, DB

BASE_DIR = Path(__file__).resolve().parent.parent


def badge(html, name):
    """The 'N sessions' button on the row of database `name`, or None."""
    row = re.search(rf'<tr><td>{re.escape(name)}</td>.*?</tr>', html, re.S)
    match = row and re.search(r'<button[^>]*data-db-sessions="[^"]*"[^>]*>([^<]*)</button>', row.group(0))
    return (match.group(0), match.group(1).strip()) if match else None


class DatabasesListSessionsTests(AdminFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.staff)
        self.held = []
        self.addCleanup(lambda: [c.close() for c in self.held])
        with admin_connect("postgres") as admin:                                       # a database of its own: nothing else connects to it,
            admin.execute(f"CREATE DATABASE {DB}")                                      # so the counts do not depend on what other tests left open

    def fragment(self, client=None):
        return (client or self.client).get(reverse("detail"), {"type": "databases", "server": self.server.pk}).content.decode()

    def connect(self, database):
        conn = psycopg.connect(host=TARGET["HOST"], port=TARGET["PORT"], dbname=database, user=TARGET["USER"], password=TARGET["PASSWORD"],
                               application_name="pgdas_t_sessions", autocommit=True)
        self.held.append(conn)
        conn.execute("SELECT 1")
        return conn

    def test_every_row_shows_its_count_also_zero_and_the_button_knows_the_database_and_server(self):
        html = self.fragment()
        found = badge(html, DB)
        self.assertIsNotNone(found, "the row of the scratch database has no sessions button")
        tag, text = found
        self.assertEqual(text, "0 sessions")
        self.assertNotIn("busy", tag)                                                    # nothing connected: not highlighted
        self.assertIn(f'data-db-sessions="{DB}"', tag)
        self.assertIn(f'data-server="{self.server.pk}"', tag)
        self.assertIn('type="button"', tag)
        for name in re.findall(r'<tr><td>([^<]+)</td><td class="muted">', html):          # every listed database has one
            self.assertIsNotNone(badge(html, name), name)

    def test_a_connected_session_is_counted_and_the_button_is_highlighted(self):
        self.connect(DB)
        tag, text = badge(self.fragment(), DB)
        self.assertEqual(text, "1 session")
        self.assertIn("busy", tag)
        self.connect(DB)
        self.assertEqual(badge(self.fragment(), DB)[1], "2 sessions")

    def test_the_list_and_the_monitor_data_agree_and_ending_the_session_clears_it(self):
        conn = self.connect(DB)
        pid = conn.info.backend_pid
        with pg.connect(self.server) as c:
            self.assertEqual({d["name"]: d["sessions"] for d in pg.list_databases(c)}[DB], 1)
        data = self.client.get(reverse("monitor_data"), {"server": self.server.pk}).json()
        mine = [s for s in data["sessions"] if s["database"] == DB and s["application"] == "pgdas_t_sessions"]
        self.assertEqual([s["pid"] for s in mine], [pid])                                 # what the popup filters and lists
        reply = self.client.post(reverse("monitor_stop"), {"server": self.server.pk, "pid": pid, "how": "end"}).json()
        self.assertTrue(reply["ok"], reply)
        self.assertTrue(AuditEntry.objects.filter(action="session.end", ok=True).exists())
        self.assertEqual(badge(self.fragment(), DB)[1], "0 sessions")

    def test_other_databases_sessions_are_not_counted_here(self):
        other = f"{DB}_other"
        self.addCleanup(lambda: admin_connect("postgres").execute(f"DROP DATABASE IF EXISTS {other} WITH (FORCE)"))
        with admin_connect("postgres") as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {other} WITH (FORCE)")                 # a run that was stopped half way may have left it
            admin.execute(f"CREATE DATABASE {other}")
        self.connect(DB)
        html = self.fragment()
        self.assertEqual(badge(html, DB)[1], "1 session")
        self.assertEqual(badge(html, other)[1], "0 sessions")

    def test_people_who_are_not_staff_do_not_get_the_buttons(self):
        plain = Client()
        plain.force_login(get_user_model().objects.create_user("plain", password="pw-12345-abc"))
        html = self.fragment(plain)
        self.assertNotIn("data-db-sessions", html)


class SessionsPopupTests(AdminFixture, TestCase):
    def page(self, staff=True):
        user = self.staff if staff else get_user_model().objects.create_user("viewer", password="pw-12345-abc")
        client = Client()
        client.force_login(user)
        return client.get("/").content.decode()

    def test_staff_get_the_popup_its_script_and_its_styles(self):
        html = self.page()
        dialog = html[html.index('id="dlgDbSessions"'):]
        dialog = dialog[:dialog.index('id="pgToast"')]
        self.assertIn('data-data-url="/monitor/data/"', dialog)
        self.assertIn('data-stop-url="/monitor/stop/"', dialog)
        for needle in ('id="dbsTitle"', 'id="dbsSummary"', 'id="dbsRows"', 'id="dbsMessage"', "data-dbs-refresh", "data-dbs-end-all", "data-dbs-close",
                       "<th>PID</th>", "<th>APPLICATION</th>", "<th>STATE</th>", "<th>LAST QUERY</th>", "Disconnect the other sessions first"):
            self.assertIn(needle, dialog)
        self.assertIn('src="/static/db_sessions.js"', html)
        self.assertIn('href="/static/dbsessions.css"', html)
        self.assertLess(html.index("admin_dialogs.js"), html.index("db_sessions.js") + 1)

    def test_others_do_not_get_it(self):
        html = self.page(staff=False)
        self.assertNotIn("dlgDbSessions", html)
        self.assertNotIn("db_sessions.js", html)

    def test_the_script_filters_by_database_asks_twice_and_never_uses_the_browser_box_or_html(self):
        script = (BASE_DIR / "static" / "db_sessions.js").read_text(encoding="utf-8")
        for needle in ("s.database === db", "twoStep(", "Click again to end", "Click again to end ALL", 'how: how', "pgdas:changed", 'what: "databases"',
                       "setInterval", "clearInterval", '"Escape"', "dataUrl + \"?server=\"", "textContent"):
            self.assertIn(needle, script)
        self.assertNotIn("innerHTML", script)
        self.assertNotRegex(script, r"\bconfirm\(")                                         # no plain browser OK box
        self.assertNotIn("window.alert", script)
        css = (BASE_DIR / "static" / "dbsessions.css").read_text(encoding="utf-8")
        for needle in (".sessions-btn.busy", ".cyber-dialog.dlg.dbs", ".btn.armed"):
            self.assertIn(needle, css)
