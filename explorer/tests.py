"""Tests run against a real PostgreSQL server: the one PgDeck browses. PgDeck's own data is in SQLite.

The PostgreSQL server comes from PGDECK_TEST_PG_HOST / _PORT / _USER / _PASSWORD (falling back to the older
PGDECK_DB_* names); its login needs CREATEDB. A scratch database, pgdeck_test_target, is created and dropped here.
"""
import os
import unittest

import psycopg
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from audit.models import AuditEntry
from servers.models import ServerConnection

from . import pg


def _env(*names, default=""):
    return next((os.environ[name] for name in names if os.environ.get(name)), default)


TARGET = {
    "HOST": _env("PGDECK_TEST_PG_HOST", "PGDECK_DB_HOST", default="localhost"),
    "PORT": _env("PGDECK_TEST_PG_PORT", "PGDECK_DB_PORT", default="5432"),
    "USER": _env("PGDECK_TEST_PG_USER", "PGDECK_DB_USER", default="postgres"),
    "PASSWORD": _env("PGDECK_TEST_PG_PASSWORD", "PGDECK_DB_PASSWORD"),
    "NAME": "pgdeck_test_target",
}


def admin_connect(dbname):
    return psycopg.connect(host=TARGET["HOST"], port=TARGET["PORT"], dbname=dbname, user=TARGET["USER"],
                           password=TARGET["PASSWORD"], autocommit=True, connect_timeout=5)


def make_server(**overrides):
    server = ServerConnection(
        name="test", host=TARGET["HOST"], port=int(TARGET["PORT"]),
        database=TARGET["NAME"], username=TARGET["USER"], sslmode="prefer", **overrides)
    server.set_password(TARGET["PASSWORD"])
    server.save()
    return server


class PgFixture:
    """Creates a scratch PostgreSQL database with sample objects, which PgDeck then browses."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            with admin_connect("postgres") as conn:
                conn.execute(f"DROP DATABASE IF EXISTS {TARGET['NAME']} WITH (FORCE)")
                conn.execute(f"CREATE DATABASE {TARGET['NAME']}")
        except psycopg.Error as error:
            raise unittest.SkipTest(f"needs a PostgreSQL server whose login can create databases "
                                    f"(set PGDECK_TEST_PG_*): {str(error).splitlines()[0]}")
        with admin_connect(TARGET["NAME"]) as conn:
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
            conn.execute("CREATE MATERIALIZED VIEW pgdeck_demo.people_mv AS SELECT id, name FROM pgdeck_demo.people WITH DATA")
            conn.execute("CREATE UNIQUE INDEX people_mv_id ON pgdeck_demo.people_mv (id)")
            conn.execute("CREATE FUNCTION pgdeck_demo.add_one(a int) RETURNS int LANGUAGE sql IMMUTABLE AS 'SELECT a + 1'")
            conn.execute("CREATE FUNCTION pgdeck_demo.add_one(a numeric) RETURNS numeric LANGUAGE sql IMMUTABLE AS 'SELECT a + 1'")
            conn.execute("CREATE PROCEDURE pgdeck_demo.do_nothing() LANGUAGE sql AS 'SELECT 1'")
            conn.execute("CREATE AGGREGATE pgdeck_demo.mysum(int) (sfunc = int4pl, stype = int)")
            conn.execute("CREATE SEQUENCE pgdeck_demo.demo_seq START 10 INCREMENT 5 MAXVALUE 1000 CYCLE")
            conn.execute("CREATE TYPE pgdeck_demo.mood AS ENUM ('sad', 'ok', 'it''s great')")
            conn.execute("CREATE TYPE pgdeck_demo.pair AS (a int, b text)")
            conn.execute("CREATE DOMAIN pgdeck_demo.positive_int AS integer NOT NULL DEFAULT 1 CONSTRAINT positive CHECK (VALUE > 0)")
            conn.execute("CREATE TYPE pgdeck_demo.span AS RANGE (subtype = int4)")
            conn.execute("CREATE FUNCTION pgdeck_demo.touch() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$")
            conn.execute("CREATE TRIGGER people_touch BEFORE INSERT OR UPDATE ON pgdeck_demo.people "
                         "FOR EACH ROW EXECUTE FUNCTION pgdeck_demo.touch()")
            conn.execute("CREATE TRIGGER people_audit AFTER DELETE ON pgdeck_demo.people "
                         "FOR EACH STATEMENT EXECUTE FUNCTION pgdeck_demo.touch()")
            conn.execute("CREATE TABLE pgdeck_demo.fresh (a int)")
            conn.execute("ALTER TABLE pgdeck_demo.fresh SET (autovacuum_enabled = false)")
            conn.execute("ANALYZE pgdeck_demo.people")
            # Objects that need extra privileges: each group is created on its own, and a test that needs a group
            # that could not be created is skipped (see `need`).
            cls.extras = set()
            for key, statements in (
                ("event", ["CREATE FUNCTION pgdeck_demo.on_ddl() RETURNS event_trigger LANGUAGE plpgsql AS $$ BEGIN NULL; END $$",
                           "CREATE EVENT TRIGGER pgdeck_ddl_watch ON ddl_command_end WHEN TAG IN ('CREATE TABLE', 'ALTER TABLE') "
                           "EXECUTE FUNCTION pgdeck_demo.on_ddl()"]),
                ("pub", ["CREATE PUBLICATION pgdeck_pub FOR TABLE pgdeck_demo.people, pgdeck_demo.orders "
                         "WITH (publish = 'insert, update')"]),
                ("pub_all", ["CREATE PUBLICATION pgdeck_pub_all FOR ALL TABLES"]),
                ("pub_schema", ["CREATE PUBLICATION pgdeck_pub_schema FOR TABLES IN SCHEMA pgdeck_demo"]),
            ):
                try:
                    for statement in statements:
                        conn.execute(statement)
                    cls.extras.add(key)
                except psycopg.Error:
                    pass
        try:
            with admin_connect("postgres") as conn:   # roles belong to the whole server, not to one database
                for statement in ("DROP ROLE IF EXISTS pgdeck_member", "DROP ROLE IF EXISTS pgdeck_group",
                                  "CREATE ROLE pgdeck_group NOLOGIN",
                                  "CREATE ROLE pgdeck_member LOGIN CONNECTION LIMIT 5 VALID UNTIL '2031-01-01 00:00:00+00' IN ROLE pgdeck_group",
                                  "COMMENT ON ROLE pgdeck_member IS 'demo role'"):
                    conn.execute(statement)
            cls.extras.add("roles")
        except psycopg.Error:
            pass

    @classmethod
    def tearDownClass(cls):
        with admin_connect("postgres") as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {TARGET['NAME']} WITH (FORCE)")   # the scratch database goes away
            conn.execute("DROP ROLE IF EXISTS pgdeck_member")
            conn.execute("DROP ROLE IF EXISTS pgdeck_group")
        super().tearDownClass()

    def need(self, *keys):
        missing = [key for key in keys if key not in self.extras]
        if missing:
            self.skipTest(f"this PostgreSQL login could not create: {', '.join(missing)}")


class PgLayerTests(PgFixture, TestCase):
    def test_connection_reports_version(self):
        self.assertRegex(pg.test_connection(make_server()), r"^\d+")

    def test_without_a_saved_database_the_connection_picks_one_that_works(self):
        server = make_server()
        for saved in ("", "no_such_database_xyz"):       # blank, or a database that does not exist on this server
            server.database = saved
            with self.subTest(saved=saved), pg.connect(server) as conn:
                self.assertEqual(conn.execute("SELECT current_database()").fetchone()[0], "postgres")
        server.database = TARGET["NAME"]   # an older entry that does name a usable database
        with pg.connect(server) as conn:
            self.assertEqual(conn.execute("SELECT current_database()").fetchone()[0], TARGET["NAME"])

    def test_a_wrong_password_is_not_hidden_by_trying_other_databases(self):
        server = make_server()
        server.set_password("definitely-wrong-password")
        with self.assertRaisesRegex(pg.PgError, "password authentication failed") as caught:
            pg.test_connection(server)
        self.assertNotIn("definitely-wrong-password", str(caught.exception))

    def test_an_explicitly_chosen_database_is_never_replaced(self):
        # Opening a database from the tree must show that database's own error, not quietly use another one.
        with self.assertRaisesRegex(pg.PgError, 'database "no_such_database_xyz" does not exist'):
            with pg.connect(make_server(), "no_such_database_xyz"):
                pass

    def test_only_databases_the_login_may_connect_to_are_listed(self):
        params = TARGET
        admin = dict(host=params["HOST"] or "localhost", port=params["PORT"] or 5432, user=params["USER"],
                     password=params["PASSWORD"] or "", autocommit=True)
        try:
            with psycopg.connect(dbname=params["NAME"], **admin) as conn:
                conn.execute("DROP DATABASE IF EXISTS pgdeck_hidden")
                conn.execute("DROP DATABASE IF EXISTS pgdeck_open")
                conn.execute("DROP ROLE IF EXISTS pgdeck_limited")
                conn.execute("CREATE ROLE pgdeck_limited LOGIN PASSWORD 'limited-pw-123' NOSUPERUSER")
                conn.execute("CREATE DATABASE pgdeck_hidden")
                conn.execute("REVOKE CONNECT ON DATABASE pgdeck_hidden FROM PUBLIC")
                conn.execute("CREATE DATABASE pgdeck_open")
        except psycopg.Error:
            self.skipTest("this test needs a role that can create databases and roles")
        try:
            limited = ServerConnection(name="limited", host=admin["host"], port=int(admin["port"]), username="pgdeck_limited", sslmode="prefer")
            limited.set_password("limited-pw-123")
            with pg.connect(limited) as conn:
                names = [d["name"] for d in pg.list_databases(conn)]
            self.assertIn("pgdeck_open", names)
            self.assertNotIn("pgdeck_hidden", names)               # exists, but this login may not connect to it
            self.assertNotIn("template0", names)
            with pg.connect(make_server()) as conn:
                self.assertIn("pgdeck_hidden", [d["name"] for d in pg.list_databases(conn)])   # the admin sees it
        finally:
            with psycopg.connect(dbname=params["NAME"], **admin) as conn:
                conn.execute("DROP DATABASE IF EXISTS pgdeck_hidden")
                conn.execute("DROP DATABASE IF EXISTS pgdeck_open")
                conn.execute("DROP ROLE IF EXISTS pgdeck_limited")

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
        expected = {"people": "table", "people_view": "view", "orders": "table", "items": "table", "fresh": "table",
                    "people_mv": "materialized view", "demo_seq": "sequence", "orders_id_seq": "sequence"}
        for name, kind in expected.items():
            self.assertEqual(tables.get(name), kind, name)
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
        params = TARGET
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
        params = TARGET
        with psycopg.connect(host=params["HOST"] or "localhost", port=params["PORT"] or 5432, dbname=params["NAME"],
                             user=params["USER"], password=params["PASSWORD"] or "", autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS pgdeck_copy CASCADE")
            conn.execute("CREATE SCHEMA pgdeck_copy")
            conn.execute(ddl.replace("pgdeck_demo.items", "pgdeck_copy.items"))
            conn.execute("INSERT INTO pgdeck_copy.items (price) VALUES (10)")
            self.assertEqual(conn.execute("SELECT tax FROM pgdeck_copy.items").fetchone()[0], 1)
            conn.execute("DROP SCHEMA pgdeck_copy CASCADE")

    def replay(self, *statements, schema="pgdeck_copy"):
        """Run generated DDL in a scratch schema to prove it is valid, then drop the schema."""
        params = TARGET
        with psycopg.connect(host=params["HOST"] or "localhost", port=params["PORT"] or 5432, dbname=params["NAME"],
                             user=params["USER"], password=params["PASSWORD"] or "", autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            conn.execute(f"CREATE SCHEMA {schema}")
            try:
                for statement in statements:
                    conn.execute(statement)
            finally:
                conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")

    def test_views_and_materialized_views(self):
        with pg.connect(make_server()) as conn:
            view_info = pg.relation_info(conn, "pgdeck_demo", "people_view")
            mv_info = pg.relation_info(conn, "pgdeck_demo", "people_mv")
            view = pg.view_ddl(conn, "pgdeck_demo", "people_view")
            matview = pg.view_ddl(conn, "pgdeck_demo", "people_mv")
            columns = [c["name"] for c in pg.table_columns(conn, "pgdeck_demo", "people_mv")]
            with self.assertRaises(pg.PgError):
                pg.view_ddl(conn, "pgdeck_demo", "people")  # a table is not a view
        self.assertEqual((view_info["kind"], mv_info["kind"], mv_info["populated"]), ("view", "materialized view", True))
        self.assertIn("CREATE OR REPLACE VIEW pgdeck_demo.people_view AS", view)
        self.assertIn("CREATE MATERIALIZED VIEW pgdeck_demo.people_mv AS", matview)
        self.assertIn("WITH DATA;", matview)
        self.assertIn("CREATE UNIQUE INDEX people_mv_id", matview)
        self.assertEqual(columns, ["id", "name"])
        self.replay(view.replace("pgdeck_demo.people_view", "pgdeck_copy.people_view"),
                    matview.replace("pgdeck_demo.people_mv", "pgdeck_copy.people_mv").replace("ON pgdeck_demo.people_mv", "ON pgdeck_copy.people_mv"))

    def test_triggers(self):
        with pg.connect(make_server()) as conn:
            triggers = {t["name"]: t for t in pg.table_triggers(conn, "pgdeck_demo", "people")}
            none = pg.table_triggers(conn, "pgdeck_demo", "orders")
        self.assertEqual(set(triggers), {"people_touch", "people_audit"})
        touch = triggers["people_touch"]
        self.assertEqual((touch["timing"], touch["events"], touch["level"], touch["state"], touch["function"]),
                         ("BEFORE", "INSERT OR UPDATE", "ROW", "enabled", "touch"))
        audit = triggers["people_audit"]
        self.assertEqual((audit["timing"], audit["events"], audit["level"]), ("AFTER", "DELETE", "STATEMENT"))
        self.assertIn("EXECUTE FUNCTION pgdeck_demo.touch()", touch["definition"])
        self.assertEqual(none, [])

    def test_functions_procedures_and_aggregates(self):
        with pg.connect(make_server()) as conn:
            functions = {f["signature"]: f for f in pg.list_functions(conn, "pgdeck_demo")}
            procedures = pg.list_functions(conn, "pgdeck_demo", procedures=True)
            add_int = functions["add_one(a integer)"]
            info = pg.function_info(conn, "pgdeck_demo", add_int["oid"])
            aggregate = pg.function_info(conn, "pgdeck_demo", functions["mysum(integer)"]["oid"])
            proc = pg.function_info(conn, "pgdeck_demo", procedures[0]["oid"])
            with self.assertRaises(pg.PgError):
                pg.function_info(conn, "public", add_int["oid"])           # right oid, wrong schema
            with self.assertRaises(pg.PgError):
                pg.function_info(conn, "pgdeck_demo", "1; DROP TABLE x")   # not a number
        self.assertIn("add_one(a numeric)", functions)                      # overloads stay separate
        self.assertEqual(functions["touch()"]["returns"], "trigger")
        self.assertEqual(functions["mysum(integer)"]["kind"], "aggregate")
        self.assertEqual([p["signature"] for p in procedures], ["do_nothing()"])
        self.assertEqual(procedures[0]["returns"], "")
        self.assertEqual((info["volatility"], info["language"], info["security"], info["kind"]), ("immutable", "sql", "invoker", "function"))
        self.assertIn("CREATE OR REPLACE FUNCTION pgdeck_demo.add_one(a integer)", info["ddl"])
        self.assertTrue(aggregate["ddl"].startswith("-- mysum is an aggregate"))
        self.assertEqual(proc["kind"], "procedure")
        self.assertIn("CREATE OR REPLACE PROCEDURE pgdeck_demo.do_nothing()", proc["ddl"])
        self.replay(info["ddl"].replace("pgdeck_demo.add_one", "pgdeck_copy.add_one"))

    def test_sequences(self):
        with pg.connect(make_server()) as conn:
            demo = pg.sequence_info(conn, "pgdeck_demo", "demo_seq")
            owned = pg.sequence_info(conn, "pgdeck_demo", "orders_id_seq")
            ddl = pg.sequence_ddl(conn, "pgdeck_demo", "demo_seq")
            owned_ddl = pg.sequence_ddl(conn, "pgdeck_demo", "orders_id_seq")
            with self.assertRaises(pg.PgError):
                pg.sequence_info(conn, "pgdeck_demo", "people")
        self.assertEqual((demo["start"], demo["increment"], demo["max"], demo["cycle"], demo["type"]), (10, 5, 1000, True, "bigint"))
        self.assertEqual(owned["owned_by"], "orders.id")
        self.assertIn("INCREMENT BY 5", ddl)
        self.assertIn("\n    CYCLE;", ddl)
        self.assertIn("OWNED BY pgdeck_demo.orders.id", owned_ddl)
        self.replay(ddl.replace("pgdeck_demo.demo_seq", "pgdeck_copy.demo_seq"))

    def test_types_and_domains(self):
        with pg.connect(make_server()) as conn:
            kinds = {t["name"]: t["kind"] for t in pg.list_types(conn, "pgdeck_demo")}
            mood = pg.type_info(conn, "pgdeck_demo", "mood")
            pair = pg.type_info(conn, "pgdeck_demo", "pair")
            domain = pg.type_info(conn, "pgdeck_demo", "positive_int")
            span = pg.type_info(conn, "pgdeck_demo", "span")
            ddls = [pg.type_ddl(conn, "pgdeck_demo", name) for name in ("mood", "pair", "positive_int", "span")]
            with self.assertRaises(pg.PgError):
                pg.type_info(conn, "pgdeck_demo", "people")   # a table's row type is not listed
        self.assertEqual(kinds, {"mood": "enum", "pair": "composite", "positive_int": "domain", "span": "range"})
        self.assertEqual(mood["labels"], ["sad", "ok", "it's great"])
        self.assertEqual([f["name"] for f in pair["fields"]], ["a", "b"])
        self.assertEqual((domain["base"], domain["not_null"], domain["default"]), ("integer", True, "1"))
        self.assertEqual(domain["constraints"][0]["name"], "positive")
        self.assertEqual(span["subtype"], "integer")
        self.assertIn("'it''s great'", ddls[0])                  # a quote inside a label is escaped
        self.assertIn("CONSTRAINT positive CHECK", ddls[2])
        self.replay(*[d.replace("pgdeck_demo.", "pgdeck_copy.") for d in ddls])

    def test_catalogs_are_listed_apart_from_schemas(self):
        with pg.connect(make_server()) as conn:
            schemas = [s["name"] for s in pg.schema_rows(conn)]
            catalogs = pg.catalog_rows(conn)
        self.assertIn("pgdeck_demo", schemas)
        self.assertNotIn("pg_catalog", schemas)             # the system catalogs live under Catalogs
        self.assertNotIn("information_schema", schemas)
        self.assertEqual([c["name"] for c in catalogs], ["information_schema", "pg_catalog"])
        self.assertTrue(all(c["owner"] for c in catalogs))

    def test_roles(self):
        self.need("roles")
        with pg.connect(make_server()) as conn:
            plain = {r["name"]: r for r in pg.list_roles(conn)}
            everything = {r["name"] for r in pg.list_roles(conn, system=True)}
            member = pg.role_info(conn, "pgdeck_member")
            group = pg.role_info(conn, "pgdeck_group")
            ddl = pg.role_ddl(conn, "pgdeck_member")
            with self.assertRaises(pg.PgError):
                pg.role_info(conn, "no_such_role_xyz")
        self.assertEqual((plain["pgdeck_group"]["kind"], plain["pgdeck_member"]["kind"]), ("group", "login"))
        self.assertFalse([name for name in plain if name.startswith("pg_")])            # built-in roles hidden by default
        self.assertIn("pg_read_all_data", everything)
        self.assertEqual((member["login"], member["superuser"], member["connection_limit"]), (True, False, 5))
        self.assertTrue(member["valid_until"].startswith("2031-01-01"))
        self.assertEqual((member["member_of"], member["comment"]), (["pgdeck_group"], "demo role"))
        self.assertEqual((group["members"], group["login"], group["connection_limit"]), (["pgdeck_member"], False, -1))
        for text in ("CREATE ROLE pgdeck_member WITH", "NOSUPERUSER", "LOGIN", "CONNECTION LIMIT 5", "VALID UNTIL '2031-01-01",
                     "GRANT pgdeck_group TO pgdeck_member;"):
            self.assertIn(text, ddl)
        self.assertNotIn("PASSWORD", ddl.upper())                                       # a password is never read or shown
        with admin_connect("postgres") as conn:                                         # the DDL really runs
            try:
                conn.execute(ddl.replace("pgdeck_member", "pgdeck_member_copy"))
                self.assertTrue(conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'pgdeck_member_copy'").fetchone())
            finally:
                conn.execute("DROP ROLE IF EXISTS pgdeck_member_copy")

    def test_tablespaces(self):
        with pg.connect(make_server()) as conn:
            spaces = {t["name"]: t for t in pg.list_tablespaces(conn)}
            default = pg.tablespace_info(conn, "pg_default")
            ddl = pg.tablespace_ddl(conn, "pg_default")
            with self.assertRaises(pg.PgError):
                pg.tablespace_info(conn, "no_such_space")
        self.assertTrue({"pg_default", "pg_global"} <= set(spaces))
        self.assertNotEqual(spaces["pg_default"]["size_text"], "—")                     # a size, not "unknown"
        self.assertEqual((default["location"], default["options"]), ("", []))
        self.assertTrue(ddl.startswith("-- pg_default is built into PostgreSQL"))

    def test_extensions(self):
        with pg.connect(make_server()) as conn:
            extensions = {e["name"]: e for e in pg.list_extensions(conn)}
            info = pg.extension_info(conn, "plpgsql")
            ddl = pg.extension_ddl(conn, "plpgsql")
            with self.assertRaises(pg.PgError):
                pg.extension_info(conn, "not_installed_xyz")
        self.assertEqual(extensions["plpgsql"]["schema"], "pg_catalog")
        self.assertEqual((info["name"], info["relocatable"], info["update_available"]), ("plpgsql", False, False))
        self.assertTrue(info["owner"] and info["version"] and info["comment"])
        self.assertIn("CREATE EXTENSION plpgsql\n    WITH SCHEMA pg_catalog\n    VERSION '", ddl)

    def test_event_triggers(self):
        self.need("event")
        with pg.connect(make_server()) as conn:
            triggers = {t["name"]: t for t in pg.list_event_triggers(conn)}
            ddl = pg.event_trigger_ddl(conn, "pgdeck_ddl_watch")
            with self.assertRaises(pg.PgError):
                pg.event_trigger_info(conn, "nope")
        trigger = triggers["pgdeck_ddl_watch"]
        self.assertEqual((trigger["event"], trigger["state"], trigger["function"]), ("ddl_command_end", "enabled", "pgdeck_demo.on_ddl"))
        self.assertEqual(trigger["tags"], ["CREATE TABLE", "ALTER TABLE"])       # kept in the order they were written
        self.assertIn("WHEN TAG IN ('CREATE TABLE', 'ALTER TABLE')", ddl)
        self.assertIn("EXECUTE FUNCTION pgdeck_demo.on_ddl();", ddl)
        with admin_connect(TARGET["NAME"]) as conn:                                      # the DDL really runs
            conn.execute("DROP EVENT TRIGGER pgdeck_ddl_watch")
            conn.execute(ddl)
            self.assertTrue(conn.execute("SELECT 1 FROM pg_event_trigger WHERE evtname = 'pgdeck_ddl_watch'").fetchone())

    def test_publications(self):
        self.need("pub")
        with pg.connect(make_server()) as conn:
            listed = {p["name"]: p for p in pg.list_publications(conn)}
            info = pg.publication_info(conn, "pgdeck_pub")
            ddl = pg.publication_ddl(conn, "pgdeck_pub")
            with self.assertRaises(pg.PgError):
                pg.publication_info(conn, "nope")
        self.assertEqual((listed["pgdeck_pub"]["table_count"], listed["pgdeck_pub"]["all_tables"]), (2, False))
        self.assertEqual((info["publishes"], info["tables"]), (["insert", "update"], ["pgdeck_demo.orders", "pgdeck_demo.people"]))
        self.assertIn("CREATE PUBLICATION pgdeck_pub\n    FOR TABLE pgdeck_demo.orders, pgdeck_demo.people", ddl)
        self.assertIn("WITH (publish = 'insert, update');", ddl)
        with admin_connect(TARGET["NAME"]) as conn:                                      # the DDL really runs
            try:
                conn.execute(ddl.replace("pgdeck_pub\n", "pgdeck_pub_copy\n"))
            finally:
                conn.execute("DROP PUBLICATION IF EXISTS pgdeck_pub_copy")

    def test_all_tables_and_schema_publications(self):
        self.need("pub_all", "pub_schema")
        with pg.connect(make_server()) as conn:
            all_tables = pg.publication_info(conn, "pgdeck_pub_all")
            all_ddl = pg.publication_ddl(conn, "pgdeck_pub_all")
            schema = pg.publication_info(conn, "pgdeck_pub_schema")
            schema_ddl = pg.publication_ddl(conn, "pgdeck_pub_schema")
        self.assertTrue(all_tables["all_tables"])
        self.assertIn("FOR ALL TABLES", all_ddl)
        self.assertEqual(schema["schemas"], ["pgdeck_demo"])
        self.assertIn("FOR TABLES IN SCHEMA pgdeck_demo", schema_ddl)
        self.assertNotIn("FOR TABLE ", schema_ddl)                # the schema's tables are not repeated one by one

    def test_system_objects_are_hidden_unless_asked_for(self):
        """The `system` switch now covers extension-owned objects and built-in pg_* roles (catalogs are always listed)."""
        with pg.connect(make_server()) as conn:
            plain_roles = [r["name"] for r in pg.list_roles(conn)]
            system_roles = [r["name"] for r in pg.list_roles(conn, system=True)]
        self.assertFalse([name for name in plain_roles if name.startswith("pg_")])
        self.assertTrue([name for name in system_roles if name.startswith("pg_")])

    def test_extension_objects_are_hidden_unless_system_is_on(self):
        params = TARGET
        with psycopg.connect(host=params["HOST"] or "localhost", port=params["PORT"] or 5432, dbname=params["NAME"],
                             user=params["USER"], password=params["PASSWORD"] or "", autocommit=True) as conn:
            try:
                conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA pgdeck_demo")
            except psycopg.Error:
                self.skipTest("pg_trgm is not available on this PostgreSQL server")
        with pg.connect(make_server()) as conn:
            plain = {f["name"] for f in pg.list_functions(conn, "pgdeck_demo")}
            everything = {f["name"] for f in pg.list_functions(conn, "pgdeck_demo", system=True)}
        self.assertNotIn("similarity", plain)
        self.assertIn("similarity", everything)
        self.assertIn("add_one", plain)        # the schema's own functions are always listed

    def test_never_analyzed_table_has_no_estimate_instead_of_zero(self):  # noqa: kept after the step 2 tests
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
        self.user.is_staff = True
        self.user.save()
        html = self.client.get(reverse("server_list")).content.decode()
        sidebar = html[html.index("<aside"):html.index("</aside>")]
        main = html[html.index("<main"):]
        self.assertIn('id="tree"', sidebar)
        self.assertNotIn('id="tree"', main)
        self.assertIn('id="detail"', main)
        self.assertIn("has-tree", html[:html.index("<aside")])
        # No separate "Object Browser" menu item; the Audit log link comes after (below) the tree.
        self.assertNotIn('<span class="ico">▦</span> Object Browser', sidebar)
        self.assertGreater(sidebar.index("Audit log"), sidebar.index('id="tree"'))

    def test_overview_header_belongs_to_the_servers_node_only(self):
        # The title, Add server button and tiles sit in one block that explorer.js shows only for the top node.
        self.user.is_staff = True
        self.user.save()
        plain = self.client.get(reverse("server_list")).content.decode()
        block = plain[plain.index('id="servers-overview"'):plain.index('id="explorer"')]
        for text in ("Object Browser</h1>", "Add server", "saved connections", "recorded in the audit log"):
            self.assertIn(text, block)
            self.assertNotIn(text, plain[plain.index('id="explorer"'):])  # nothing of it outside that block
        self.assertNotIn('id="servers-overview" hidden', plain)
        # Opened straight onto a server (deep link / old address): the block starts hidden.
        deep = self.client.get(reverse("server_list"), {"server": self.server.pk}).content.decode()
        self.assertIn('id="servers-overview" hidden', deep)
        table = self.client.get(reverse("server_list"), {"type": "table", "server": self.server.pk, "db": "d",
                                                         "schema": "s", "table": "t"}).content.decode()
        self.assertIn('id="servers-overview" hidden', table)
        top = self.client.get(reverse("server_list"), {"type": "servers"}).content.decode()
        self.assertNotIn('id="servers-overview" hidden', top)
        script = open("static/explorer.js", encoding="utf-8").read()
        self.assertIn('overview.hidden = node.type !== "servers"', script)

    def test_audit_link_is_staff_only_in_the_sidebar(self):
        html = self.client.get(reverse("server_list")).content.decode()
        self.assertNotIn("Audit log", html[html.index("<aside"):html.index("</aside>")])

    def test_every_page_has_the_tree_in_its_sidebar_but_only_the_browser_has_details(self):
        self.user.is_staff = True
        self.user.save()
        pages = {"audit": reverse("audit_list"), "add server": reverse("server_add"),
                 "sql runner": reverse("sql_runner", args=[self.server.pk]),
                 "edit server": reverse("server_edit", args=[self.server.pk]), "browser": reverse("server_list")}
        for name, url in pages.items():
            html = self.client.get(url).content.decode()
            sidebar = html[html.index("<aside"):html.index("</aside>")]
            with self.subTest(page=name):
                self.assertIn('id="tree"', sidebar)
                self.assertIn(f'data-tree-url="{reverse("tree")}"', sidebar)
                self.assertIn(f'data-home-url="{reverse("server_list")}"', sidebar)
                self.assertIn("explorer.js", html)
                self.assertEqual('id="detail"' in html, name == "browser")  # the details pane exists only on the browser page
                self.assertIn(f'<a class="brand" href="{reverse("server_list")}"', sidebar)

    def test_the_tree_script_goes_to_the_browser_from_other_pages_and_restores_the_path(self):
        script = open("static/explorer.js", encoding="utf-8").read()
        self.assertIn('window.location.href = homeUrl + "?" + query(node)', script)   # other page: go to the browser
        self.assertIn("function pathTo(target)", script)                              # browser page: open ancestors, select node
        self.assertIn('twist.addEventListener("click"', script)                       # the arrow only opens/closes
        for needle in ("SCHEMA_FOLDER_OF", '"views"', '"matviews"', '"functions"', '"procedures"', '"sequences"', '"types"',
                       '"triggers"', 'params.set("system", "1")', "pgdeck.showSystem",
                       "SERVER_FOLDER_OF", "DB_FOLDER_OF", "CATALOG_SCHEMAS", '"roles"', '"tablespaces"', '"catalogs"',
                       '"extensions"', '"eventtriggers"', '"publications"'):
            self.assertIn(needle, script)
        html = self.client.get(reverse("server_list")).content.decode()
        self.assertIn('id="showSystem"', html[html.index("<aside"):html.index("</aside>")])   # the "system" switch

    def test_sidebar_is_resizable_and_collapsible_and_remembers_it(self):
        html = self.client.get(reverse("server_list")).content.decode()
        sidebar = html[html.index("<aside"):html.index("</aside>")]
        self.assertIn('id="sidebarResizer"', sidebar)
        self.assertIn('role="separator"', sidebar)
        self.assertIn('tabindex="0"', sidebar[sidebar.index("sidebarResizer"):])        # keyboard reachable
        head = html[:html.index("</head>")]
        self.assertIn("pgdeck.sidebarWidth", head)                                       # restored before first paint
        self.assertIn("pgdeck.sidebarCollapsed", head)
        script = open("static/app.js", encoding="utf-8").read()
        for needle in ('"pointerdown"', '"dblclick"', "ArrowLeft", "sidebar-collapsed", "localStorage.setItem"):
            self.assertIn(needle, script)
        css = open("static/pgdeck.css", encoding="utf-8").read()
        for needle in ("--sidebar-w", ".sidebar-resizer", "::-webkit-scrollbar-thumb", "html.sidebar-collapsed aside"):
            self.assertIn(needle, css)

    def test_interface_sounds_from_the_template_are_wired_in(self):
        html = self.client.get(reverse("server_list")).content.decode()
        header = html[html.index("<header"):html.index("</header>")]
        self.assertIn('id="soundToggle"', header)                     # the speaker (mute) button
        self.assertIn('aria-pressed="false"', header)
        self.assertIn("sound.js", html)
        script = open("static/sound.js", encoding="utf-8").read()
        for needle in ("click: [420, 580]", "success: [620, 960]", "error: [180, 120]",   # the template's tones
                       'createOscillator', "exponentialRampToValueAtTime(0.055", "pgdeck.sound", "localStorage.setItem",
                       'closest("button, a, .metric, .row, input[type=checkbox], select")', "Mute sounds"):
            self.assertIn(needle, script)
        self.assertNotIn(".mp3", script)                               # no audio files: tones are generated
        explorer = open("static/explorer.js", encoding="utf-8").read()
        self.assertIn('pgdeckSound.play("error")', explorer)           # failed loads sound the error tone
        css = open("static/pgdeck.css", encoding="utf-8").read()
        self.assertIn("#soundToggle.muted .slash", css)

    def test_login_page_gets_click_sounds_but_no_mute_button(self):
        self.client.logout()
        page = self.client.get(reverse("login"))
        self.assertContains(page, "sound.js")
        self.assertNotContains(page, 'id="soundToggle"')

    def test_login_page_has_no_tree(self):
        self.client.logout()
        page = self.client.get(reverse("login"))
        self.assertNotContains(page, 'id="tree"')
        self.assertNotContains(page, "explorer.js")

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
        self.assertEqual([n["label"] for n in self.nodes(type="server", server=second.pk)],
                         ["Databases", "Roles", "Tablespaces"])  # listing them needs no connection

    def test_unknown_server_is_an_error_not_a_crash(self):
        response = self.client.get(reverse("tree"), {"type": "databases", "server": 99999})
        self.assertEqual(response.status_code, 502)
        self.assertIn("not saved", response.json()["error"])
        self.assertContains(self.detail(type="server", server=99999), "not saved")
        self.assertContains(self.detail(type="databases", server=""), "not saved")

    def test_tree_walks_server_to_indexes(self):
        db = self.server.database
        self.assertEqual([n["type"] for n in self.nodes(type="server")], ["databases", "roles", "tablespaces"])
        self.assertIn(db, [n["label"] for n in self.nodes(type="databases")])
        self.assertEqual([n["label"] for n in self.nodes(type="database", db=db)][-1], "Schemas")   # (after the new folders)
        self.assertIn("pgdeck_demo", [n["label"] for n in self.nodes(type="schemas", db=db)])
        self.assertEqual([n["label"] for n in self.nodes(type="schema", db=db, schema="pgdeck_demo")],
                         ["Tables", "Views", "Materialized Views", "Functions", "Procedures", "Sequences", "Types / Domains"])
        tables = {n["label"] for n in self.nodes(type="tables", db=db, schema="pgdeck_demo")}
        self.assertEqual(tables, {"people", "orders", "items", "fresh"})  # views are not tables
        place = {"db": db, "schema": "pgdeck_demo", "table": "orders"}
        self.assertEqual([n["label"] for n in self.nodes(type="table", **place)], ["Columns", "Constraints", "Indexes", "Triggers"])
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
        self.assertEqual(response.json()["error"], 'database "no_such_db_xyz" does not exist')   # just the reason, once
        self.assertNotIn("Multiple connection attempts", response.json()["error"])

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

    def test_schema_folders_list_their_objects(self):
        db, where = self.server.database, {"db": self.server.database, "schema": "pgdeck_demo"}
        views = self.detail(type="views", **where)
        self.assertContains(views, "Views in pgdeck_demo")
        self.assertContains(views, "people_view")
        self.assertNotContains(views, "people_mv")
        self.assertContains(self.detail(type="matviews", **where), "people_mv")
        self.assertContains(self.detail(type="sequences", **where), "demo_seq")
        functions = self.detail(type="functions", **where)
        for text in ("add_one", "a numeric", "mysum", "aggregate", "trigger"):
            self.assertContains(functions, text)
        self.assertContains(self.detail(type="procedures", **where), "do_nothing")
        types = self.detail(type="types", **where)
        for text in ("mood", "enum", "positive_int", "domain", "pair", "span", "range"):
            self.assertContains(types, text)

    def test_tree_has_the_new_object_nodes(self):
        db, where = self.server.database, {"db": self.server.database, "schema": "pgdeck_demo"}
        views = self.nodes(type="views", **where)
        self.assertEqual([(n["label"], n["type"]) for n in views], [("people_view", "view")])
        self.assertEqual([n["label"] for n in self.nodes(type="view", table="people_view", **where)], ["Columns"])
        self.assertEqual([n["rel"] for n in self.nodes(type="view", table="people_view", **where)], ["view"])
        self.assertEqual([n["label"] for n in self.nodes(type="matview", table="people_mv", **where)], ["Columns", "Indexes"])
        self.assertEqual([n["label"] for n in self.nodes(type="matviews", **where)], ["people_mv"])
        sequences = {n["label"]: n for n in self.nodes(type="sequences", **where)}
        self.assertIn("demo_seq", sequences)
        self.assertFalse(sequences["demo_seq"]["expandable"])
        functions = {n["label"]: n for n in self.nodes(type="functions", **where)}
        self.assertIn("add_one(a integer)", functions)
        self.assertIn("add_one(a numeric)", functions)
        self.assertTrue(functions["add_one(a integer)"]["oid"].isdigit())
        self.assertEqual(functions["add_one(a integer)"]["hint"], "integer")
        self.assertEqual([n["label"] for n in self.nodes(type="procedures", **where)], ["do_nothing()"])
        types = {n["label"]: n["hint"] for n in self.nodes(type="types", **where)}
        self.assertEqual(types, {"mood": "enum", "pair": "composite", "positive_int": "domain", "span": "range"})
        triggers = {n["label"]: n["hint"] for n in self.nodes(type="triggers", table="people", rel="table", **where)}
        self.assertEqual(triggers, {"people_audit": "after delete", "people_touch": "before insert or update"})
        self.assertEqual(self.nodes(type="triggers", table="people", rel="table", **where)[0]["rel"], "table")

    def test_view_matview_and_sequence_tabs(self):
        where = {"db": self.server.database, "schema": "pgdeck_demo"}
        view = self.detail(type="view", table="people_view", **where)
        self.assertContains(view, "View")
        self.assertNotContains(view, ">Constraints<")           # a view has no constraints, indexes or triggers
        self.assertNotContains(view, ">Triggers<")
        self.assertContains(self.detail(type="view", table="people_view", tab="sql", **where), "CREATE OR REPLACE VIEW")
        self.assertContains(self.detail(type="view", table="people_view", tab="data", **where), "person 1")
        matview = self.detail(type="matview", table="people_mv", **where)
        self.assertContains(matview, ">Indexes<")
        self.assertContains(matview, "Populated")
        self.assertContains(self.detail(type="columns", table="people_mv", rel="matview", **where), "name")
        self.assertContains(self.detail(type="index", table="people_mv", rel="matview", **where), "people_mv_id")
        self.assertContains(self.detail(type="matview", table="people_mv", tab="sql", **where), "CREATE MATERIALIZED VIEW")
        sequence = self.detail(type="sequence", table="demo_seq", **where)
        for text in ("Increment", "1000", "not used yet", "Cycle"):
            self.assertContains(sequence, text)
        self.assertContains(self.detail(type="sequence", table="demo_seq", tab="sql", **where), "CREATE SEQUENCE")
        self.assertNotContains(sequence, ">Data<")

    def test_trigger_tab_and_leaf(self):
        where = {"db": self.server.database, "schema": "pgdeck_demo", "table": "people"}
        tab = self.detail(type="table", tab="triggers", **where)
        for text in ("people_touch", "BEFORE", "INSERT OR UPDATE", "ROW", "enabled", "EXECUTE FUNCTION pgdeck_demo.touch()", "people_audit"):
            self.assertContains(tab, text)
        self.assertContains(self.detail(type="trigger", name="people_touch", rel="table", **where), "people_touch")
        self.assertContains(self.detail(type="triggers", rel="table", **where), "STATEMENT")

    def test_function_and_type_details(self):
        where = {"db": self.server.database, "schema": "pgdeck_demo"}
        oid = next(n["oid"] for n in self.nodes(type="functions", **where) if n["label"] == "add_one(a integer)")
        props = self.detail(type="function", oid=oid, **where)
        for text in ("immutable", "invoker", "Language", "sql", "integer"):
            self.assertContains(props, text)
        self.assertContains(self.detail(type="function", oid=oid, tab="sql", **where), "CREATE OR REPLACE FUNCTION pgdeck_demo.add_one")
        proc_oid = self.nodes(type="procedures", **where)[0]["oid"]
        self.assertContains(self.detail(type="procedure", oid=proc_oid, **where), "Procedure")
        self.assertContains(self.detail(type="function", oid="abc", **where), "was not found")
        self.assertContains(self.detail(type="function", oid="999999999", **where), "was not found")
        enum = self.detail(type="type", name="mood", **where)
        for text in ("enum", "sad", "it&#x27;s great"):
            self.assertContains(enum, text)
        self.assertContains(self.detail(type="type", name="mood", tab="sql", **where), "CREATE TYPE pgdeck_demo.mood AS ENUM")
        domain = self.detail(type="type", name="positive_int", **where)
        for text in ("domain", "Base type", "integer", "positive", "Not null"):
            self.assertContains(domain, text)
        self.assertContains(self.detail(type="type", name="pair", **where), "composite")
        self.assertContains(self.detail(type="type", name="span", **where), "Subtype")
        self.assertContains(self.detail(type="type", name="people", **where), "was not found")

    def test_system_flag_reaches_the_tree_and_the_detail_links(self):
        db = self.server.database
        self.need("roles")
        plain = [n["label"] for n in self.nodes(type="roles")]
        system = [n["label"] for n in self.nodes(type="roles", system="1")]
        self.assertFalse([name for name in plain if name.startswith("pg_")])      # built-in roles are hidden by default
        self.assertIn("pg_read_all_data", system)
        where = {"db": db, "schema": "pgdeck_demo", "table": "people", "type": "table"}
        self.assertNotContains(self.detail(**where), "system=1")
        self.assertContains(self.detail(system="1", **where), "system=1")   # tab links keep the flag
        catalog = self.detail(type="tables", db=db, schema="pg_catalog")
        self.assertContains(catalog, "pg_class")

    def test_server_and_database_have_their_extra_folders(self):
        db = self.server.database
        self.assertEqual([(n["label"], n["type"]) for n in self.nodes(type="server")],
                         [("Databases", "databases"), ("Roles", "roles"), ("Tablespaces", "tablespaces")])
        self.assertEqual([n["label"] for n in self.nodes(type="database", db=db)],
                         ["Catalogs", "Extensions", "Event Triggers", "Publications", "Schemas"])
        catalogs = self.nodes(type="catalogs", db=db)
        self.assertEqual([(n["label"], n["type"], n["schema"]) for n in catalogs],
                         [("information_schema", "catalog", "information_schema"), ("pg_catalog", "catalog", "pg_catalog")])
        self.assertEqual([n["label"] for n in self.nodes(type="catalog", db=db, schema="pg_catalog")],
                         ["Tables", "Views", "Materialized Views", "Functions", "Procedures", "Sequences", "Types / Domains"])
        schemas = [n["label"] for n in self.nodes(type="schemas", db=db)]
        self.assertIn("pgdeck_demo", schemas)
        self.assertNotIn("pg_catalog", schemas)                       # catalogs are not repeated under Schemas
        tables = [n["label"] for n in self.nodes(type="tables", db=db, schema="pg_catalog")]
        self.assertIn("pg_class", tables)                              # a catalog opens like any schema

    def test_roles_and_tablespaces_nodes(self):
        self.need("roles")
        roles = {n["label"]: n for n in self.nodes(type="roles")}
        self.assertEqual((roles["pgdeck_member"]["hint"], roles["pgdeck_group"]["hint"]), ("login", "group"))
        self.assertFalse(roles["pgdeck_member"]["expandable"])
        self.assertEqual(roles["pgdeck_member"]["name"], "pgdeck_member")
        spaces = {n["label"]: n for n in self.nodes(type="tablespaces")}
        self.assertTrue({"pg_default", "pg_global"} <= set(spaces))
        self.assertNotEqual(spaces["pg_default"]["hint"], "—")

    def test_database_level_object_nodes(self):
        db = self.server.database
        extensions = {n["label"]: n for n in self.nodes(type="extensions", db=db)}
        self.assertIn("plpgsql", extensions)
        self.assertEqual(extensions["plpgsql"]["db"], db)
        self.assertTrue(extensions["plpgsql"]["hint"])                 # the version
        if "event" in self.extras:
            self.assertEqual([(n["label"], n["hint"]) for n in self.nodes(type="eventtriggers", db=db)],
                             [("pgdeck_ddl_watch", "ddl_command_end")])
        if "pub" in self.extras:
            publications = {n["label"]: n["hint"] for n in self.nodes(type="publications", db=db)}
            self.assertEqual(publications["pgdeck_pub"], "2 tables")
            if "pub_all" in self.extras:
                self.assertEqual(publications["pgdeck_pub_all"], "all tables")

    def test_details_of_the_new_folders_and_objects(self):
        db = self.server.database
        self.need("roles", "event", "pub")
        roles = self.detail(type="roles")
        for text in ("Roles on", "pgdeck_member", "login", "group"):
            self.assertContains(roles, text)
        member = self.detail(type="role", name="pgdeck_member")
        for text in ("Can log in", "Connection limit", "2031-01-01", "pgdeck_group", "demo role"):
            self.assertContains(member, text)
        self.assertContains(self.detail(type="role", name="pgdeck_member", tab="sql"), "GRANT pgdeck_group TO pgdeck_member;")
        self.assertContains(self.detail(type="role", name="no_such_role_xyz"), "was not found")
        spaces = self.detail(type="tablespaces")
        self.assertContains(spaces, "pg_default")
        self.assertContains(spaces, "(built in)")
        self.assertContains(self.detail(type="tablespace", name="pg_default"), "built in")
        self.assertContains(self.detail(type="tablespace", name="pg_default", tab="sql"), "built into PostgreSQL")
        self.assertContains(self.detail(type="catalogs", db=db), "pg_catalog")
        catalog = self.detail(type="catalog", db=db, schema="pg_catalog")
        self.assertContains(catalog, "Catalog pg_catalog")
        self.assertContains(catalog, "pg_class")
        self.assertContains(self.detail(type="extensions", db=db), "plpgsql")
        extension = self.detail(type="extension", db=db, name="plpgsql")
        for text in ("Version", "Relocatable", "pg_catalog"):
            self.assertContains(extension, text)
        self.assertContains(self.detail(type="extension", db=db, name="plpgsql", tab="sql"), "CREATE EXTENSION plpgsql")
        self.assertContains(self.detail(type="extension", db=db, name="nope"), "is not installed")
        self.assertContains(self.detail(type="eventtriggers", db=db), "pgdeck_ddl_watch")
        trigger = self.detail(type="eventtrigger", db=db, name="pgdeck_ddl_watch")
        for text in ("ddl_command_end", "CREATE TABLE", "pgdeck_demo.on_ddl"):
            self.assertContains(trigger, text)
        self.assertContains(self.detail(type="publications", db=db), "pgdeck_pub")
        publication = self.detail(type="publication", db=db, name="pgdeck_pub")
        for text in ("insert", "update", "pgdeck_demo.people", "pgdeck_demo.orders"):
            self.assertContains(publication, text)
        self.assertContains(self.detail(type="publication", db=db, name="pgdeck_pub", tab="sql"), "CREATE PUBLICATION pgdeck_pub")
        self.assertContains(self.detail(type="publication", db=db, name="nope"), "was not found")

    def test_unknown_table_shows_an_error_message(self):
        response = self.detail(type="table", db=self.server.database, schema="pgdeck_demo", table="missing", tab="data")
        self.assertContains(response, "was not found")

    def test_table_data_escapes_html(self):
        params = TARGET
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
