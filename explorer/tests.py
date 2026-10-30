"""Tests run against real PostgreSQL. The Django test database doubles as the managed server."""
import psycopg
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse

from audit.models import AuditEntry
from servers.models import ServerConnection

from . import pg


def make_server(**overrides):
    settings_dict = connection.settings_dict
    server = ServerConnection(
        name="test", host=settings_dict["HOST"] or "localhost", port=int(settings_dict["PORT"] or 5432),
        database=settings_dict["NAME"], username=settings_dict["USER"], sslmode="prefer", **overrides)
    server.set_password(settings_dict["PASSWORD"] or "")
    server.save()
    return server


class PgFixture:
    """Creates committed tables in the test database, which a separate connection can then read."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        params = connection.settings_dict
        with psycopg.connect(host=params["HOST"] or "localhost", port=params["PORT"] or 5432, dbname=params["NAME"],
                             user=params["USER"], password=params["PASSWORD"] or "", autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS pgdeck_demo CASCADE")
            conn.execute("CREATE SCHEMA pgdeck_demo")
            conn.execute("CREATE TABLE pgdeck_demo.people (id serial PRIMARY KEY, name text NOT NULL, note text, tags jsonb)")
            conn.execute("INSERT INTO pgdeck_demo.people (name, note, tags) SELECT 'person ' || g, NULL, '{\"a\": 1}' FROM generate_series(1, 120) g")
            conn.execute("CREATE VIEW pgdeck_demo.people_view AS SELECT id, name FROM pgdeck_demo.people")
            conn.execute("""CREATE TABLE pgdeck_demo.orders (
                id serial PRIMARY KEY, person_id int NOT NULL REFERENCES pgdeck_demo.people(id),
                amount numeric(10, 2) DEFAULT 0 CHECK (amount >= 0), ref text UNIQUE)""")
            conn.execute("CREATE INDEX orders_person_idx ON pgdeck_demo.orders (person_id)")
            conn.execute("""CREATE TABLE pgdeck_demo.items (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, price numeric(8, 2) NOT NULL,
                tax numeric(8, 2) GENERATED ALWAYS AS (price * 0.1) STORED)""")
            conn.execute("CREATE TABLE pgdeck_demo.fresh (a int)")
            conn.execute("ALTER TABLE pgdeck_demo.fresh SET (autovacuum_enabled = false)")
            conn.execute("ANALYZE pgdeck_demo.people")

    @classmethod
    def tearDownClass(cls):
        params = connection.settings_dict
        with psycopg.connect(host=params["HOST"] or "localhost", port=params["PORT"] or 5432, dbname=params["NAME"],
                             user=params["USER"], password=params["PASSWORD"] or "", autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS pgdeck_demo CASCADE")
        super().tearDownClass()


class PgLayerTests(PgFixture, TestCase):
    def test_connection_reports_version(self):
        self.assertRegex(pg.test_connection(make_server()), r"^\d+")

    def test_schemas_exclude_system_schemas(self):
        with pg.connect(make_server()) as conn:
            schemas = pg.list_schemas(conn)
        self.assertIn("pgdeck_demo", schemas)
        self.assertNotIn("pg_catalog", schemas)
        self.assertNotIn("information_schema", schemas)

    def test_tables_and_views_with_columns(self):
        with pg.connect(make_server()) as conn:
            tables = {item["name"]: item["kind"] for item in pg.list_tables(conn, "pgdeck_demo")}
            columns = {c["name"]: c for c in pg.table_columns(conn, "pgdeck_demo", "people")}
            view_columns = [c["name"] for c in pg.table_columns(conn, "pgdeck_demo", "people_view")]
        self.assertEqual(tables, {"people": "table", "people_view": "view", "orders": "table",
                                  "items": "table", "fresh": "table"})
        self.assertFalse(columns["name"]["nullable"])
        self.assertTrue(columns["note"]["nullable"])
        self.assertEqual(view_columns, ["id", "name"])

    def test_databases_include_the_current_one_with_owner_and_size(self):
        server = make_server()
        with pg.connect(server) as conn:
            databases = {d["name"]: d for d in pg.list_databases(conn)}
        current = databases[server.database]
        self.assertTrue(current["owner"])
        self.assertGreater(current["size"], 0)
        self.assertNotIn("template0", databases)

    def test_constraints_list_primary_foreign_unique_and_check(self):
        with pg.connect(make_server()) as conn:
            found = {c["type"]: c for c in pg.table_constraints(conn, "pgdeck_demo", "orders")}
        self.assertEqual(set(found), {"PRIMARY KEY", "FOREIGN KEY", "UNIQUE", "CHECK"})
        self.assertIn("REFERENCES pgdeck_demo.people(id)", found["FOREIGN KEY"]["definition"])
        self.assertIn("amount >= (0)::numeric", found["CHECK"]["definition"])  # as PostgreSQL normalizes it

    def test_indexes_flag_primary_and_unique(self):
        with pg.connect(make_server()) as conn:
            indexes = {i["name"]: i for i in pg.table_indexes(conn, "pgdeck_demo", "orders")}
        self.assertTrue(indexes["orders_pkey"]["primary"])
        self.assertTrue(indexes["orders_ref_key"]["unique"])
        self.assertFalse(indexes["orders_person_idx"]["unique"])
        self.assertIn("(person_id)", indexes["orders_person_idx"]["definition"])

    def test_table_info_and_missing_table(self):
        with pg.connect(make_server()) as conn:
            info = pg.table_info(conn, "pgdeck_demo", "people")
            with self.assertRaises(pg.PgError):
                pg.table_info(conn, "pgdeck_demo", "people_view")  # views are not tables
        self.assertEqual(info["kind"], "table")
        self.assertTrue(info["size_text"])

    def test_ddl_recreates_the_table(self):
        with pg.connect(make_server()) as conn:
            ddl = pg.table_ddl(conn, "pgdeck_demo", "orders")
        self.assertIn("CREATE TABLE pgdeck_demo.orders (", ddl)
        self.assertIn("NOT NULL", ddl)
        self.assertIn("id serial NOT NULL", ddl)  # not nextval('..._seq'), which could not be replayed elsewhere
        self.assertNotIn("nextval", ddl)
        self.assertIn("CONSTRAINT orders_pkey PRIMARY KEY (id)", ddl)
        self.assertIn("CREATE INDEX orders_person_idx", ddl)
        self.assertNotIn("CREATE UNIQUE INDEX orders_ref_key", ddl)  # already part of the UNIQUE constraint
        # The DDL must actually run: replay it into a scratch schema.
        params = connection.settings_dict
        with psycopg.connect(host=params["HOST"] or "localhost", port=params["PORT"] or 5432, dbname=params["NAME"],
                             user=params["USER"], password=params["PASSWORD"] or "", autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS pgdeck_copy CASCADE")
            conn.execute("CREATE SCHEMA pgdeck_copy")
            conn.execute(ddl.replace("pgdeck_demo.orders", "pgdeck_copy.orders"))
            self.assertEqual(conn.execute("SELECT count(*) FROM pgdeck_copy.orders").fetchone()[0], 0)
            conn.execute("DROP SCHEMA pgdeck_copy CASCADE")

    def test_identity_and_generated_columns_appear_in_ddl_and_replay(self):
        with pg.connect(make_server()) as conn:
            columns = {c["name"]: c for c in pg.table_columns(conn, "pgdeck_demo", "items")}
            ddl = pg.table_ddl(conn, "pgdeck_demo", "items")
        self.assertEqual(columns["id"]["identity"], "a")
        self.assertEqual(columns["tax"]["generated"], "s")
        self.assertIn("id bigint GENERATED ALWAYS AS IDENTITY NOT NULL", ddl)
        self.assertIn("GENERATED ALWAYS AS ((price * 0.1)) STORED", ddl)
        params = connection.settings_dict
        with psycopg.connect(host=params["HOST"] or "localhost", port=params["PORT"] or 5432, dbname=params["NAME"],
                             user=params["USER"], password=params["PASSWORD"] or "", autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS pgdeck_copy CASCADE")
            conn.execute("CREATE SCHEMA pgdeck_copy")
            conn.execute(ddl.replace("pgdeck_demo.items", "pgdeck_copy.items"))
            conn.execute("INSERT INTO pgdeck_copy.items (price) VALUES (10)")
            self.assertEqual(conn.execute("SELECT tax FROM pgdeck_copy.items").fetchone()[0], 1)
            conn.execute("DROP SCHEMA pgdeck_copy CASCADE")

    def test_never_analyzed_table_has_no_estimate_instead_of_zero(self):
        with pg.connect(make_server()) as conn:
            fresh = pg.table_info(conn, "pgdeck_demo", "fresh")["estimate"]
            people = pg.table_info(conn, "pgdeck_demo", "people")["estimate"]
            listed = {t["name"]: t["estimate"] for t in pg.list_tables(conn, "pgdeck_demo")}
        self.assertIsNone(fresh)
        self.assertEqual(people, 120)
        self.assertIsNone(listed["fresh"])

    def test_row_paging_and_null_display(self):
        with pg.connect(make_server()) as conn:
            first = pg.fetch_rows(conn, "pgdeck_demo", "people", 1)
            last = pg.fetch_rows(conn, "pgdeck_demo", "people", 3)
        self.assertEqual(len(first["rows"]), pg.PAGE_SIZE)
        self.assertTrue(first["has_next"])
        self.assertEqual(len(last["rows"]), 20)
        self.assertFalse(last["has_next"])
        self.assertIsNone(first["rows"][0][2])  # NULL note
        self.assertEqual(first["rows"][0][3], '{"a": 1}')

    def test_unknown_table_is_an_error_not_a_crash(self):
        with self.assertRaises(pg.PgError), pg.connect(make_server()) as conn:
            pg.fetch_rows(conn, "pgdeck_demo", "missing", 1)

    def test_sql_select_returns_rows(self):
        result = pg.run_sql(make_server(), "SELECT id, name FROM pgdeck_demo.people ORDER BY id LIMIT 3")
        self.assertEqual(result["columns"], ["id", "name"])
        self.assertEqual(result["rows"][0], ["1", "person 1"])

    def test_writes_are_rejected_by_the_database(self):
        server = make_server()
        for statement in ("DELETE FROM pgdeck_demo.people", "DROP TABLE pgdeck_demo.people",
                          "INSERT INTO pgdeck_demo.people (name) VALUES ('x')", "CREATE TABLE pgdeck_demo.t (a int)"):
            with self.subTest(statement=statement), self.assertRaisesRegex(pg.PgError, "read-only"):
                pg.run_sql(server, statement)
        self.assertEqual(pg.run_sql(server, "SELECT count(*) FROM pgdeck_demo.people")["rows"], [["120"]])

    def test_row_cap_marks_truncated(self):
        with override_settings(MAX_ROWS=10):
            result = pg.run_sql(make_server(), "SELECT * FROM generate_series(1, 50)")
        self.assertEqual(result["count"], 10)
        self.assertTrue(result["truncated"])

    def test_statement_timeout(self):
        with override_settings(STATEMENT_TIMEOUT_MS=200), self.assertRaisesRegex(pg.PgError, "timeout"):
            pg.run_sql(make_server(), "SELECT pg_sleep(5)")

    def test_bad_credentials_never_leak_the_password(self):
        server = make_server()
        server.set_password("definitely-wrong-password")
        with self.assertRaises(pg.PgError) as caught:
            pg.test_connection(server)
        self.assertNotIn("definitely-wrong-password", str(caught.exception))


class ExplorerViewTests(PgFixture, TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("ana", password="pw-12345-abc")
        self.server = make_server()
        self.client.force_login(self.user)

    def nodes(self, **params):
        params.setdefault("server", self.server.pk)
        response = self.client.get(reverse("tree"), params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["nodes"]

    def detail(self, **params):
        params.setdefault("server", self.server.pk)
        return self.client.get(reverse("detail"), params)

    def test_pages_require_login(self):
        self.client.logout()
        pk = self.server.pk
        for url in (reverse("server_list"), reverse("tree"), reverse("detail"),
                    reverse("explorer", args=[pk]), reverse("sql_runner", args=[pk])):
            self.assertEqual(self.client.get(url).status_code, 302, url)

    def test_home_page_is_the_single_object_browser(self):
        response = self.client.get(reverse("server_list"))
        self.assertContains(response, 'id="tree"')
        self.assertContains(response, reverse("tree"))
        self.assertContains(response, reverse("detail"))
        self.assertNotContains(response, "<table")  # the server list is loaded into the detail pane, not a separate page

    def test_the_tree_lives_in_the_sidebar_and_details_in_the_main_area(self):
        html = self.client.get(reverse("server_list")).content.decode()
        sidebar = html[html.index("<aside"):html.index("</aside>")]
        main = html[html.index("<main"):]
        self.assertIn('id="tree"', sidebar)
        self.assertNotIn('id="tree"', main)
        self.assertIn('id="detail"', main)
        self.assertIn("has-tree", html[:html.index("<aside")])

    def test_other_pages_keep_a_plain_sidebar_without_the_tree(self):
        self.user.is_staff = True
        self.user.save()
        for url in (reverse("audit_list"), reverse("server_add"), reverse("sql_runner", args=[self.server.pk])):
            page = self.client.get(url)
            self.assertNotContains(page, 'id="tree"')
            self.assertContains(page, "Object Browser")  # the link back to the browser

    def test_old_per_server_address_opens_the_browser_with_that_server_selected(self):
        response = self.client.get(reverse("explorer", args=[self.server.pk]))
        self.assertRedirects(response, f"{reverse('server_list')}?server={self.server.pk}")
        self.assertContains(self.client.get(reverse("server_list"), {"server": self.server.pk}),
                            f'data-preselect="{self.server.pk}"')

    def test_servers_is_the_top_node_and_lists_every_saved_server(self):
        second = ServerConnection.objects.create(name="Second", host="db2.example.test", username="u")
        top = self.nodes(type="servers")
        self.assertEqual({n["label"] for n in top}, {"test", "Second"})
        self.assertTrue(all(n["type"] == "server" and n["expandable"] for n in top))
        self.assertEqual({n["server"] for n in top}, {str(self.server.pk), str(second.pk)})
        self.assertEqual([n["label"] for n in self.nodes(type="server", server=second.pk)], ["Databases"])  # no connection needed

    def test_unknown_server_is_an_error_not_a_crash(self):
        response = self.client.get(reverse("tree"), {"type": "databases", "server": 99999})
        self.assertEqual(response.status_code, 502)
        self.assertIn("not saved", response.json()["error"])
        self.assertContains(self.detail(type="server", server=99999), "not saved")
        self.assertContains(self.detail(type="databases", server=""), "not saved")

    def test_tree_walks_server_to_indexes(self):
        db = self.server.database
        self.assertEqual([n["type"] for n in self.nodes(type="server")], ["databases"])
        self.assertIn(db, [n["label"] for n in self.nodes(type="databases")])
        self.assertEqual([n["label"] for n in self.nodes(type="database", db=db)], ["Schemas"])
        self.assertIn("pgdeck_demo", [n["label"] for n in self.nodes(type="schemas", db=db)])
        self.assertEqual([n["label"] for n in self.nodes(type="schema", db=db, schema="pgdeck_demo")], ["Tables"])
        tables = {n["label"] for n in self.nodes(type="tables", db=db, schema="pgdeck_demo")}
        self.assertEqual(tables, {"people", "orders", "items", "fresh"})  # views are not tables
        place = {"db": db, "schema": "pgdeck_demo", "table": "orders"}
        self.assertEqual([n["label"] for n in self.nodes(type="table", **place)], ["Columns", "Constraints", "Indexes"])
        columns = [n["label"] for n in self.nodes(type="columns", **place)]
        self.assertEqual(columns[0], "id : integer")
        constraints = {n["label"]: n["hint"] for n in self.nodes(type="constraints", **place)}
        self.assertEqual(constraints["orders_pkey"], "primary key")
        indexes = {n["label"]: n["hint"] for n in self.nodes(type="indexes", **place)}
        self.assertEqual(indexes["orders_person_idx"], "")
        self.assertEqual(indexes["orders_ref_key"], "unique")
        leaf = self.nodes(type="indexes", **place)[0]
        self.assertFalse(leaf["expandable"])

    def test_tree_reports_unreachable_database_as_error_json(self):
        response = self.client.get(reverse("tree"), {"type": "schemas", "server": self.server.pk, "db": "no_such_db_xyz"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("no_such_db_xyz", response.json()["error"])

    def test_detail_for_each_level(self):
        db = self.server.database
        listing = self.detail(type="servers", server="")
        self.assertContains(listing, "test")
        self.assertContains(listing, reverse("server_test", args=[self.server.pk]))
        self.assertContains(listing, 'data-open-server="%s"' % self.server.pk)
        self.assertContains(self.detail(type="server"), "PostgreSQL")
        self.assertContains(self.detail(type="server"), "Connection")
        self.assertContains(self.detail(type="databases"), db)
        self.assertContains(self.detail(type="database", db=db), "Encoding")
        self.assertContains(self.detail(type="schemas", db=db), "pgdeck_demo")
        self.assertContains(self.detail(type="schema", db=db, schema="pgdeck_demo"), "orders")
        self.assertContains(self.detail(type="tables", db=db, schema="pgdeck_demo"), "people")

    def test_table_tabs(self):
        place = {"type": "table", "db": self.server.database, "schema": "pgdeck_demo", "table": "orders"}
        first = self.detail(**place)
        self.assertContains(first, "Total size")
        self.assertContains(first, f"server={self.server.pk}")  # tab links keep the server, so they work from the one browser page
        self.assertContains(first, f"<dd>{self.server.name}</dd>")
        self.assertContains(self.detail(**place, tab="columns"), "numeric(10,2)")
        self.assertContains(self.detail(**place, tab="constraints"), "FOREIGN KEY")
        self.assertContains(self.detail(**place, tab="indexes"), "orders_person_idx")
        self.assertContains(self.detail(**place, tab="sql"), "CREATE TABLE pgdeck_demo.orders")
        data = self.detail(type="table", db=self.server.database, schema="pgdeck_demo", table="people", tab="data", page=3)
        self.assertContains(data, "person 101")
        self.assertNotContains(data, "Next →")
        self.assertContains(data, "Previous")

    def test_leaf_nodes_open_their_own_tab(self):
        place = {"db": self.server.database, "schema": "pgdeck_demo", "table": "orders"}
        self.assertContains(self.detail(type="index", **place), "orders_ref_key")
        self.assertContains(self.detail(type="constraint", **place), "PRIMARY KEY")

    def test_unknown_table_shows_an_error_message(self):
        response = self.detail(type="table", db=self.server.database, schema="pgdeck_demo", table="missing", tab="data")
        self.assertContains(response, "was not found")

    def test_table_data_escapes_html(self):
        params = connection.settings_dict
        with psycopg.connect(host=params["HOST"] or "localhost", port=params["PORT"] or 5432, dbname=params["NAME"],
                             user=params["USER"], password=params["PASSWORD"] or "", autocommit=True) as conn:
            conn.execute("INSERT INTO pgdeck_demo.people (name) VALUES ('<script>alert(1)</script>')")
        response = self.detail(type="table", db=self.server.database, schema="pgdeck_demo", table="people", tab="data", page=3)
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert(1)")

    def test_hostile_identifiers_are_not_executed(self):
        response = self.detail(type="table", db=self.server.database, schema="pgdeck_demo",
                               table='people"; DROP TABLE pgdeck_demo.people; --', tab="data")
        self.assertContains(response, "was not found")
        with pg.connect(self.server) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM pgdeck_demo.people").fetchone()[0], 120)

    def test_sql_run_is_audited_for_success_and_failure(self):
        url = reverse("sql_runner", args=[self.server.pk])
        ok = self.client.post(url, {"sql": "SELECT 1 AS one"})
        self.assertContains(ok, "one")
        bad = self.client.post(url, {"sql": "DELETE FROM pgdeck_demo.people"})
        self.assertContains(bad, "read-only")
        entries = list(AuditEntry.objects.filter(action="sql.run").order_by("pk").values_list("ok", "username"))
        self.assertEqual(entries, [(True, "ana"), (False, "ana")])
