"""Monitor: sessions, blocked sessions, slow queries; cancelling a query and ending a session. Uses real extra sessions."""
import threading
import time

import psycopg
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from audit.models import AuditEntry
from explorer.tests import TARGET, admin_connect
from explorer.tests_writes import WriteFixture

from . import queries

APP = "pgdeck_t_monitor"


def other_session(autocommit=False):
    """Another login to the scratch database, named so it can be told apart in the monitor."""
    return psycopg.connect(host=TARGET["HOST"], port=TARGET["PORT"], dbname=TARGET["NAME"], user=TARGET["USER"],
                           password=TARGET["PASSWORD"], autocommit=autocommit, application_name=APP, connect_timeout=5)


class MonitorFixture(WriteFixture):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.staff)
        self.opened = []
        self.addCleanup(self.close_all)

    def session(self, autocommit=False):
        conn = other_session(autocommit)
        self.opened.append(conn)
        return conn

    def close_all(self):
        for conn in self.opened:
            try:
                conn.close()
            except psycopg.Error:
                pass

    def mine(self, data=None):
        data = data or queries.snapshot(self.server)
        return [s for s in data["sessions"] if s["application"] == APP]

    def wait_for(self, check, seconds=8):
        end = time.time() + seconds
        while time.time() < end:
            result = check()
            if result:
                return result
            time.sleep(0.2)
        self.fail("PostgreSQL never showed the expected state.")

    def run_in_thread(self, conn, sql):
        outcome = {}

        def work():
            try:
                conn.execute(sql)
                outcome["done"] = True
            except psycopg.Error as error:
                outcome["error"] = error
        thread = threading.Thread(target=work)
        thread.start()
        self.addCleanup(thread.join, 10)
        return outcome


class SnapshotTests(MonitorFixture, TestCase):
    def test_an_idle_in_transaction_session_is_listed_with_its_state_and_last_query(self):
        conn = self.session()
        conn.execute("UPDATE pgdeck_w.logged SET what = 'held' WHERE id = 1")
        found = self.wait_for(self.mine)
        self.assertEqual(found[0]["state"], "idle in transaction")
        self.assertIn("UPDATE pgdeck_w.logged", found[0]["query"])
        self.assertEqual(found[0]["user"], TARGET["USER"])
        self.assertIn(found[0]["pid"], [s["pid"] for s in queries.snapshot(self.server)["idle_in_transaction"]])

    def test_the_monitor_does_not_list_its_own_connection(self):
        data = queries.snapshot(self.server)
        self.assertNotIn("PGDAS", [s["application"] for s in data["sessions"]])

    def test_a_blocked_session_shows_who_blocks_it(self):
        holder = self.session()
        holder.execute("UPDATE pgdeck_w.logged SET what = 'held' WHERE id = 1")
        waiter = self.session(autocommit=True)
        self.run_in_thread(waiter, "UPDATE pgdeck_w.logged SET what = 'wait' WHERE id = 1")
        locks = self.wait_for(lambda: queries.snapshot(self.server)["locks"])
        self.assertEqual(len(locks), 1)
        self.assertEqual(locks[0]["blocking_pid"], holder.info.backend_pid)
        self.assertEqual(locks[0]["blocked_pid"], waiter.info.backend_pid)
        self.assertIn("what = 'wait'", locks[0]["blocked_query"])
        holder.rollback()                                            # lets the waiter finish

    def test_slow_queries_are_the_active_ones_over_the_limit(self):
        conn = self.session(autocommit=True)
        self.run_in_thread(conn, "SELECT pg_sleep(4)")
        self.wait_for(lambda: [s for s in self.mine() if s["state"] == "active"])
        time.sleep(1.3)
        data = queries.snapshot(self.server, slow_after=1)
        self.assertIn(conn.info.backend_pid, [s["pid"] for s in data["slow"]])
        self.assertNotIn(conn.info.backend_pid, [s["pid"] for s in queries.snapshot(self.server, slow_after=60)["slow"]])

    def test_the_statements_part_says_why_when_the_extension_is_missing(self):
        data = queries.snapshot(self.server)
        self.assertTrue(data["statements"] is not None or "pg_stat_statements" in data["statements_note"])

    def test_the_data_page_returns_json_for_staff_only(self):
        data = self.client.get(reverse("monitor_data"), {"server": self.server.pk}).json()
        self.assertTrue(data["ok"])
        self.assertEqual(self.client.get(reverse("monitor_data"), {"server": 0}).status_code, 404)
        plain = Client()
        plain.force_login(get_user_model().objects.create_user("plain", password="pw-12345-abc"))
        self.assertEqual(plain.get(reverse("monitor")).status_code, 302)
        self.assertEqual(plain.get(reverse("monitor_data"), {"server": self.server.pk}).status_code, 302)
        self.assertEqual(self.client.get(reverse("monitor")).status_code, 200)


class StopTests(MonitorFixture, TestCase):
    def stop(self, pid, how):
        return self.client.post(reverse("monitor_stop"), {"server": self.server.pk, "pid": pid, "how": how})

    def test_cancel_stops_the_query_but_keeps_the_session(self):
        conn = self.session(autocommit=True)
        outcome = self.run_in_thread(conn, "SELECT pg_sleep(30)")
        self.wait_for(lambda: [s for s in self.mine() if s["state"] == "active"])
        response = self.stop(conn.info.backend_pid, "cancel")
        self.assertTrue(response.json()["ok"], response.content)
        self.wait_for(lambda: "error" in outcome)
        self.assertIsInstance(outcome["error"], psycopg.errors.QueryCanceled)
        self.assertFalse(conn.closed)
        self.assertTrue(AuditEntry.objects.filter(action="session.cancel", ok=True).exists())

    def test_end_closes_the_session_and_rolls_back_its_transaction(self):
        conn = self.session()
        conn.execute("UPDATE pgdeck_w.logged SET what = 'held' WHERE id = 1")
        pid = conn.info.backend_pid
        self.wait_for(self.mine)
        self.assertTrue(self.stop(pid, "end").json()["ok"])
        self.wait_for(lambda: pid not in [s["pid"] for s in queries.snapshot(self.server)["sessions"]])
        self.assertEqual(self.query("SELECT what FROM pgdeck_w.logged WHERE id = 1")[0][0], "a")
        self.assertTrue(AuditEntry.objects.filter(action="session.end", ok=True).exists())

    def test_a_session_that_is_gone_or_a_bad_pid_is_refused_plainly(self):
        self.assertEqual(self.stop(2147483000, "end").status_code, 404)
        self.assertEqual(self.stop("abc", "end").status_code, 400)

    def test_a_login_without_the_right_gets_postgresqls_reason(self):
        from servers.models import ServerConnection
        with admin_connect(TARGET["NAME"]) as conn:
            conn.execute("DROP ROLE IF EXISTS pgdeck_t_mon")
            conn.execute("CREATE ROLE pgdeck_t_mon LOGIN PASSWORD 'mon-pw-1'")
        self.addCleanup(lambda: admin_connect(TARGET["NAME"]).execute("DROP ROLE IF EXISTS pgdeck_t_mon"))
        limited = ServerConnection(name="limited", host=TARGET["HOST"], port=int(TARGET["PORT"]), database=TARGET["NAME"],
                                   username="pgdeck_t_mon", sslmode="prefer")
        limited.set_password("mon-pw-1")
        limited.save()
        conn = self.session()
        conn.execute("SELECT 1")
        self.wait_for(self.mine)
        response = self.client.post(reverse("monitor_stop"), {"server": limited.pk, "pid": conn.info.backend_pid, "how": "end"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("permission denied", response.json()["error"])
        self.assertFalse(conn.closed)
        self.assertTrue(AuditEntry.objects.filter(action="session.end", ok=False).exists())
