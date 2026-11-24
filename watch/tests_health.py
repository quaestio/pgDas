"""Table and index health: the findings logic (synthetic data), the collector against PostgreSQL, and the page and endpoints."""
import time
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from access_control.models import Installation, Membership, Organization, OrganizationServer
from explorer import tests as shared
from servers.models import ServerConnection

from . import health

from django.utils import timezone

NOW = timezone.now().isoformat()


def table(**over):
    row = {"schema": "public", "table": "t", "total": 10 ** 8, "heap": 8 * 10 ** 7, "indexes": 2 * 10 ** 7, "toast": 0, "live": 100000, "dead": 0, "mod_since_analyze": 0,
           "ins_since_vacuum": 0, "last_vacuum": NOW, "last_autovacuum": NOW, "last_analyze": NOW, "last_autoanalyze": NOW, "vacuum_count": 1, "autovacuum_count": 1,
           "analyze_count": 1, "autoanalyze_count": 1, "seq_scan": 0, "seq_tup_read": 0, "idx_scan": 100, "idx_tup_fetch": 100, "inserted": 0, "updated": 0, "deleted": 0,
           "hot_updated": 0, "xid_age": 1000, "mxid_age": 0, "options": [], "has_primary_key": True, "kind": "r", "heap_read": 0, "heap_hit": 0, "idx_read": 0, "idx_hit": 0,
           "last_seq_scan": None, "last_idx_scan": None}
    row.update(over)
    return row


def index(**over):
    row = {"schema": "public", "table": "t", "index": "t_a_idx", "unique": False, "primary": False, "valid": True, "size": 50 * 1024 * 1024, "scans": 10, "tup_read": 1,
           "tup_fetch": 1, "definition": "CREATE INDEX t_a_idx ON public.t (a)", "table_oid": 1, "key": "1", "opclass": "1", "options": "0", "predicate": "", "expressions": "",
           "constraints": 0, "last_scan": None}
    row.update(over)
    return row


def data(tables=(), indexes=(), **over):
    base = {"database": "d", "version": 170000, "freeze_max_age": 200_000_000, "limit": 150, "tables": list(tables), "indexes": list(indexes), "running_vacuums": [],
            "pgstattuple": False, "table_count": len(tables), "index_count": len(indexes), "stats_reset": None, "stats_age_days": 30.0, "notes": [], "truncated": False}
    base.update(over)
    return base


def kinds(found):
    return [(f["level"], f["kind"], f["object"]) for f in found]


class FindingsTests(SimpleTestCase):
    def test_a_healthy_database_has_no_findings(self):
        self.assertEqual(health.findings(data([table()], [index()])), [])

    def test_dead_rows_warn_then_become_critical_and_carry_the_vacuum_sql(self):
        warn = health.findings(data([table(live=8000, dead=2000)]))
        self.assertEqual(kinds(warn), [("warning", "dead_rows", "public.t")])
        self.assertEqual(warn[0]["sql"], 'VACUUM (ANALYZE) "public"."t";')
        crit = health.findings(data([table(live=100000, dead=300000)]))
        self.assertEqual(crit[0]["level"], "critical")
        self.assertEqual(health.findings(data([table(live=8000, dead=500)])), [])             # too few to matter
        self.assertEqual(health.findings(data([table(live=1000000, dead=100000)])), [])       # a small share

    def test_names_with_quotes_are_quoted_in_the_sql(self):
        found = health.findings(data([table(schema='My"S', table='odd name', live=1000, dead=4000)]))
        self.assertEqual(found[0]["sql"], 'VACUUM (ANALYZE) "My""S"."odd name";')

    def test_stale_statistics_only_when_many_rows_changed_and_not_analyzed_recently(self):
        self.assertEqual(kinds(health.findings(data([table(mod_since_analyze=50000, last_analyze=None, last_autoanalyze=None)]))), [("info", "stale_statistics", "public.t")])
        self.assertEqual(health.findings(data([table(mod_since_analyze=50000)])), [])          # analyzed just now

    def test_a_table_close_to_the_forced_vacuum_limit(self):
        half = health.findings(data([table(xid_age=110_000_000)]))
        self.assertEqual((half[0]["level"], half[0]["kind"]), ("warning", "freeze_age"))
        self.assertEqual(health.findings(data([table(xid_age=170_000_000)]))[0]["level"], "critical")
        self.assertEqual(health.findings(data([table(xid_age=50_000_000)])), [])

    def test_sequential_scans_cache_hot_updates_and_missing_primary_key_are_notes(self):
        found = health.findings(data([table(seq_scan=500, seq_tup_read=50_000_000, idx_scan=10, heap_read=50_000, heap_hit=10_000, updated=50_000, hot_updated=1000,
                                            has_primary_key=False)]))
        self.assertEqual(sorted(f["kind"] for f in found), ["cache", "hot_updates", "no_primary_key", "sequential_scans"])
        self.assertTrue(all(f["level"] == "info" for f in found))

    def test_an_unused_index_needs_size_age_and_no_constraint(self):
        unused = index(scans=0)
        self.assertEqual(kinds(health.findings(data([], [unused]))), [("warning", "index_unused", "public.t_a_idx")])
        self.assertEqual(health.findings(data([], [unused], stats_age_days=2.0)), [])         # statistics too young to say
        self.assertEqual(health.findings(data([], [index(scans=0, unique=True)])), [])
        self.assertEqual(health.findings(data([], [index(scans=0, primary=True)])), [])
        self.assertEqual(health.findings(data([], [index(scans=0, constraints=1)])), [])
        self.assertEqual(health.findings(data([], [index(scans=0, size=1000)])), [])
        self.assertIn('DROP INDEX CONCURRENTLY "public"."t_a_idx";', health.findings(data([], [unused]))[0]["sql"])

    def test_duplicates_keep_the_primary_or_most_used_and_flag_the_rest(self):
        keep = index(index="t_pkey", primary=True, unique=True, scans=5)
        same = index(index="t_id_idx", scans=9)
        found = health.findings(data([], [keep, same]))
        self.assertEqual(kinds(found), [("warning", "index_duplicate", "public.t_id_idx")])
        self.assertIn("t_pkey", found[0]["title"])
        both = health.findings(data([], [index(index="a", scans=1), index(index="b", scans=7)]))
        self.assertEqual(kinds(both), [("warning", "index_duplicate", "public.a")])              # the more used one stays
        different = health.findings(data([], [index(index="a"), index(index="b", key="2")]))
        self.assertEqual(different, [])

    def test_an_invalid_index_is_critical_and_comes_first(self):
        found = health.findings(data([table(live=8000, dead=2000)], [index(index="broken", valid=False)]))
        self.assertEqual(kinds(found)[0], ("critical", "index_invalid", "public.broken"))
        self.assertEqual(found[-1]["kind"], "dead_rows")

    def test_the_summary_counts_and_adds_up_the_wasted_space(self):
        d = data([table(live=8000, dead=2000)], [index(index="u", scans=0, size=2 * 1024 * 1024), index(index="a", key="2", scans=3), index(index="b", key="2", scans=1, size=3 * 1024 * 1024)])
        d["findings"] = health.findings(d)
        s = health.summary(d)
        self.assertEqual((s["needing_vacuum"], s["unused_indexes"], s["duplicate_indexes"], s["duplicate_bytes"]), (1, 1, 1, 3 * 1024 * 1024))
        self.assertEqual(s["unused_bytes"], 2 * 1024 * 1024)
        self.assertEqual(s["warnings"], 3)


@override_settings(SERVER_NETWORKS="any", ALLOWED_SERVER_HOSTS=[])
class CollectorTests(shared.PgFixture, TestCase):
    def setUp(self):
        self.server = shared.make_server()
        self.db = shared.TARGET["NAME"]
        with shared.admin_connect(self.db) as conn:
            for statement in ("DROP SCHEMA IF EXISTS pgdas_h CASCADE", "CREATE SCHEMA pgdas_h",
                              "CREATE TABLE pgdas_h.busy (id serial PRIMARY KEY, label text, n int) WITH (autovacuum_enabled = false)",
                              "INSERT INTO pgdas_h.busy (label, n) SELECT 'row ' || g, g FROM generate_series(1, 20000) g",
                              "DELETE FROM pgdas_h.busy WHERE n % 2 = 0",                                    # half the rows are now dead
                              "CREATE INDEX busy_n_idx ON pgdas_h.busy (n)", "CREATE INDEX busy_n_copy ON pgdas_h.busy (n)",
                              "CREATE INDEX busy_label_idx ON pgdas_h.busy (label)",
                              "CREATE TABLE pgdas_h.keyless (a int)", "INSERT INTO pgdas_h.keyless VALUES (1)"):
                conn.execute(statement)
        self.addCleanup(self.drop)
        time.sleep(1.6)                                                                                     # let the statistics collector catch up

    def drop(self):
        with shared.admin_connect(self.db) as conn:
            conn.execute("DROP SCHEMA IF EXISTS pgdas_h CASCADE")

    def test_the_collector_reads_tables_and_indexes_and_the_findings_follow(self):
        result = health.collect(self.server, self.db)
        self.assertEqual(result["notes"], [])
        names = {(t["schema"], t["table"]): t for t in result["tables"]}
        busy = names[("pgdas_h", "busy")]
        self.assertGreater(busy["dead"], 5000)
        self.assertTrue(busy["has_primary_key"])
        self.assertFalse(names[("pgdas_h", "keyless")]["has_primary_key"])
        indexes = {ix["index"]: ix for ix in result["indexes"]}
        self.assertIn("busy_n_idx", indexes)
        self.assertEqual(indexes["busy_n_idx"]["key"], indexes["busy_n_copy"]["key"])               # same column: the duplicate check relies on this
        found = {(f["kind"], f["object"]) for f in result["findings"]}
        self.assertIn(("dead_rows", "pgdas_h.busy"), found)
        self.assertTrue(any(kind == "index_duplicate" and obj.startswith("pgdas_h.busy_n_") for kind, obj in found))
        self.assertIn(("no_primary_key", "pgdas_h.keyless"), found)
        self.assertGreaterEqual(result["summary"]["needing_vacuum"], 1)
        self.assertEqual(result["version"] // 10000 >= 12, True)

    def test_a_missing_database_is_a_plain_error(self):
        from explorer import pg
        with self.assertRaises(pg.PgError):
            health.collect(self.server, "no_such_database_xyz")

    def test_the_limit_is_kept_within_bounds(self):
        self.assertEqual(health.collect(self.server, self.db, limit=1)["limit"], 10)
        self.assertEqual(health.collect(self.server, self.db, limit=99999)["limit"], 500)


@override_settings(SERVER_NETWORKS="any", ALLOWED_SERVER_HOSTS=[])
class HealthViewTests(shared.PgFixture, TestCase):
    def setUp(self):
        cache.clear()
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Org")
        self.admin = get_user_model().objects.create_user("a", email="a@o.test", password="pw-12345-abc", is_staff=True, is_superuser=True)
        Membership.objects.create(user=self.admin, organization=self.org, role="super_user")
        self.server = shared.make_server()
        OrganizationServer.objects.filter(server=self.server).delete()
        OrganizationServer.objects.create(server=self.server, organization=self.org)
        self.db = shared.TARGET["NAME"]
        self.client = Client()
        self.client.force_login(self.admin)

    def data(self, **params):
        return self.client.get(reverse("health_data"), {"server": self.server.pk, "db": self.db, **params})

    def test_the_page_lists_my_servers_and_links_to_the_dashboards(self):
        page = self.client.get(reverse("health"))
        for needle in ("hDatabase", "health.js", self.server.name, reverse("dashboards")):
            self.assertContains(page, needle)
        self.assertContains(self.client.get(reverse("dashboards")), reverse("health"))

    def test_the_databases_of_a_server_are_listed(self):
        payload = self.client.get(reverse("health_databases"), {"server": self.server.pk}).json()
        self.assertTrue(payload["ok"])
        self.assertIn(self.db, payload["databases"])

    def test_the_data_has_tables_indexes_findings_and_a_summary(self):
        payload = self.data().json()
        self.assertTrue(payload["ok"], payload)
        for key in ("tables", "indexes", "findings", "summary", "stats_age_days", "pgstattuple", "table_count"):
            self.assertIn(key, payload)
        self.assertEqual(payload["database"], self.db)

    def test_a_database_is_required_and_the_answer_is_cached_for_a_minute(self):
        from unittest import mock
        self.assertEqual(self.client.get(reverse("health_data"), {"server": self.server.pk}).status_code, 400)
        self.data()
        with mock.patch("watch.views.health.collect", side_effect=AssertionError("read again")):
            self.assertTrue(self.data().json()["ok"])
        with mock.patch("watch.views.health.collect", return_value={"tables": [], "findings": []}) as read:
            self.data(fresh=1)
        read.assert_called_once()

    def test_a_database_that_cannot_be_read_is_a_message(self):
        response = self.client.get(reverse("health_data"), {"server": self.server.pk, "db": "no_such_database_xyz"})
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()["ok"])

    def test_only_super_users_and_only_their_own_servers(self):
        person = get_user_model().objects.create_user("p", email="p@o.test", password="pw-12345-abc")
        Membership.objects.create(user=person, organization=self.org, role="editor")
        other = Client()
        other.force_login(person)
        self.assertEqual(other.get(reverse("health")).status_code, 403)
        self.assertIn(other.get(reverse("health_data"), {"server": self.server.pk, "db": self.db}).status_code, (403, 404))
        foreign = ServerConnection.objects.create(name="Theirs", host="h", username="u")
        OrganizationServer.objects.create(server=foreign, organization=Organization.objects.create(name="Elsewhere"))
        self.assertEqual(self.client.get(reverse("health_data"), {"server": foreign.pk, "db": "x"}).status_code, 404)
        self.assertNotContains(self.client.get(reverse("health")), "Theirs")

    def test_measuring_a_table_says_when_pgstattuple_is_missing(self):
        with shared.admin_connect(self.db) as conn:
            conn.execute("DROP EXTENSION IF EXISTS pgstattuple")
        response = self.client.post(reverse("health_bloat"), '{"server": %d, "db": "%s", "schema": "pgdas_demo", "table": "people"}' % (self.server.pk, self.db),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("pgstattuple", response.json()["error"])

    def test_measuring_a_table_with_pgstattuple_when_it_can_be_installed(self):
        try:
            with shared.admin_connect(self.db) as conn:
                conn.execute("CREATE EXTENSION IF NOT EXISTS pgstattuple")
        except Exception as error:
            self.skipTest(f"pgstattuple cannot be installed here: {error}")
        self.addCleanup(lambda: shared.admin_connect(self.db).execute("DROP EXTENSION IF EXISTS pgstattuple"))
        response = self.client.post(reverse("health_bloat"), '{"server": %d, "db": "%s", "schema": "pgdas_demo", "table": "people"}' % (self.server.pk, self.db),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertGreater(response.json()["table_len"], 0)
        self.assertEqual(self.client.post(reverse("health_bloat"), "{}", content_type="application/json").status_code, 400)

    def test_the_script_is_valid_javascript(self):
        import shutil, subprocess
        from pathlib import Path
        from django.conf import settings
        if not shutil.which("node"):
            self.skipTest("needs Node.js")
        path = (Path(settings.BASE_DIR) / "static" / "health.js").as_posix()
        done = subprocess.run(["node", "-e", f"new Function(require('fs').readFileSync('{path}', 'utf8')); console.log('ok')"], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.stdout.strip(), "ok", done.stderr)

    def test_an_employee_sees_only_their_own_database_and_cannot_fix_without_maintenance(self):
        from access_control.models import DatabaseGrant
        person = get_user_model().objects.create_user("emp", email="e@o.test", password="pw-12345-abc")
        member = Membership.objects.create(user=person, organization=self.org, role="editor")
        DatabaseGrant.objects.create(membership=member, server=self.server, database=self.db, read=True)
        emp = Client()
        emp.force_login(person)
        self.assertEqual(emp.get(reverse("health")).status_code, 200)
        self.assertEqual(emp.get(reverse("health_databases"), {"server": self.server.pk}).json()["databases"], [self.db])
        mine = emp.get(reverse("health_data"), {"server": self.server.pk, "db": self.db}).json()
        self.assertTrue(mine["ok"])
        self.assertFalse(mine["can_maintain"])
        other = emp.get(reverse("health_data"), {"server": self.server.pk, "db": "postgres"})
        self.assertEqual(other.status_code, 403)
        self.assertIn("PGDAS blocked this", other.json()["error"])
        body = {"server": self.server.sid, "db": self.db, "schema": "public", "table": "x"}
        self.assertEqual(emp.post(reverse("health_bloat"), body, content_type="application/json").status_code, 403)
        DatabaseGrant.objects.filter(membership=member).update(maintenance=True)
        cache.clear()
        self.assertTrue(emp.get(reverse("health_data"), {"server": self.server.pk, "db": self.db}).json()["can_maintain"])