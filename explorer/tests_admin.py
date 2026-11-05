"""Databases, users (roles) and grants from the SQL runner: parsing, the popup facts, the confirmation words, no passwords kept."""
import json

from django.test import SimpleTestCase
from pgdas.testing import TestCase
from django.urls import reverse

from audit.models import AuditEntry, record, redact_secrets

from . import changes, pg, server_admin
from .tests import admin_connect, TARGET
from .tests_writes import WriteFixture

ROLE, GROUP, DB = "pgdas_t_user", "pgdas_t_group", "pgdas_t_db"


class ParseTests(SimpleTestCase):
    def test_which_statements_belong_here(self):
        for text in ("CREATE DATABASE x", "alter database x rename to y", "DROP ROLE x", "create user x", "ALTER GROUP g ADD USER u",
                     "GRANT SELECT ON t TO u", "revoke all on t from u", "-- note\nCREATE ROLE r"):
            with self.subTest(text=text):
                self.assertTrue(server_admin.is_admin_statement(text))
        for text in ("CREATE TABLE t (a int)", "ALTER TABLE t ADD c int", "DROP TABLE t", "CREATE USER MAPPING FOR u SERVER s",
                     "ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO u", "SELECT 1", ""):
            with self.subTest(text=text):
                self.assertFalse(server_admin.is_admin_statement(text))

    def test_names_words_and_whether_it_runs_in_a_transaction(self):
        cases = {
            "CREATE DATABASE Shop": ("CREATE", "DATABASE", "shop", False, None),
            'CREATE DATABASE "Shop 2"': ("CREATE", "DATABASE", "Shop 2", False, None),
            "DROP DATABASE IF EXISTS shop": ("DROP", "DATABASE", "shop", False, "shop"),       # a database is confirmed by its name
            "ALTER DATABASE shop SET timezone = 'UTC'": ("ALTER", "DATABASE", "shop", True, None),
            "ALTER DATABASE shop RENAME TO shop2": ("ALTER", "DATABASE", "shop", True, "ALTER"),
            "CREATE ROLE app LOGIN PASSWORD 'secret'": ("CREATE", "ROLE", "app", True, None),
            "CREATE USER app SUPERUSER": ("CREATE", "ROLE", "app", True, "CREATE"),
            "ALTER ROLE app NOLOGIN": ("ALTER", "ROLE", "app", True, None),
            "ALTER USER app CREATEROLE": ("ALTER", "ROLE", "app", True, "ALTER"),
            "DROP ROLE app": ("DROP", "ROLE", "app", True, "DROP"),
        }
        for text, (verb, kind, name, transactional, word) in cases.items():
            with self.subTest(text=text):
                parsed = server_admin.parse(text)
                self.assertEqual((parsed["verb"], parsed["object_type"], parsed["name"], parsed["transactional"], parsed["word"]),
                                 (verb, kind, name, transactional, word))

    def test_the_password_is_hidden_in_what_is_shown(self):
        parsed = server_admin.parse("CREATE ROLE app PASSWORD 'it''s secret' LOGIN")
        self.assertIn("it''s secret", parsed["statement"])
        self.assertNotIn("secret", parsed["shown"])
        self.assertIn("PASSWORD '********'", parsed["shown"])
        for text in ("ALTER ROLE a ENCRYPTED PASSWORD 'x1'", "alter role a password E'x1'", "CREATE ROLE a WITH PASSWORD 'x1' SUPERUSER"):
            self.assertNotIn("x1", redact_secrets(text))
        self.assertEqual(redact_secrets("SELECT 'password'"), "SELECT 'password'")             # only a password being set

    def test_refused(self):
        for text, message in (("CREATE DATABASE a; CREATE DATABASE b", "only GRANT or REVOKE"),
                              ("ALTER DATABASE a SET TABLESPACE t", "tablespace"), ("CREATE DATABASE", "Write it as")):
            with self.subTest(text=text), self.assertRaisesRegex(pg.PgError, message):
                server_admin.parse(text)


class AdminFixture(WriteFixture):
    def setUp(self):
        super().setUp()
        self.clean()
        self.addCleanup(self.clean)

    def clean(self):
        with admin_connect("postgres") as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")                   # first: a test login may own it
        with admin_connect(TARGET["NAME"]) as conn:
            for role in (ROLE, GROUP, ROLE + "2"):
                if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                    conn.execute(f"DROP OWNED BY {role}")
                    conn.execute(f"DROP ROLE {role}")

    def role_exists(self, name=ROLE):
        return bool(self.query("SELECT 1 FROM pg_roles WHERE rolname = %s", name))

    def preview(self, sql):
        return changes.preview(self.server, self.db, sql)

    def execute(self, sql, fingerprint=None, confirm=None):
        return changes.execute(self.server, self.db, sql, fingerprint, confirm, self.staff)


class RoleTests(AdminFixture, TestCase):
    def test_create_role_is_previewed_and_nothing_is_created(self):
        data = self.preview(f"CREATE ROLE {ROLE} LOGIN PASSWORD 'sup3r-secret'")
        self.assertEqual((data["kind"], data["title"], data["button"], data["confirm_word"]), ("create", "CREATE ROLE?", "Create role", None))
        self.assertEqual([o["text"] for o in data["objects"]], [f"+ create role {ROLE}"])
        self.assertNotIn("sup3r-secret", json.dumps(data))                       # not in the popup either
        self.assertEqual(data["warning"], "This adds new role. It cannot be rolled back, but it can be removed again with DROP.")
        self.assertTrue(any("password" in n["text"].lower() for n in data["notes"]))
        self.assertFalse(self.role_exists())
        self.assertFalse(AuditEntry.objects.exists())

    def test_create_role_runs_and_the_password_is_never_recorded(self):
        sql = f"CREATE ROLE {ROLE} LOGIN PASSWORD 'sup3r-secret'"
        result = self.execute(sql, self.preview(sql)["fingerprint"])
        self.assertTrue(self.role_exists())
        entry = AuditEntry.objects.get(pk=result["entry_id"])
        self.assertEqual((entry.action, entry.ok, entry.payload["object_type"]), ("create", True, "ROLE"))
        self.assertNotIn("sup3r-secret", json.dumps([entry.detail, entry.payload]))
        self.assertIn("PASSWORD '********'", entry.detail)
        self.assertEqual(entry.record_label, "CREATE ROLE · 1 object")
        self.assertNotIn("sup3r-secret", " ".join(e.detail for e in AuditEntry.objects.all()))
        self.assertTrue(self.query("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", ROLE)[0][0])

    def test_a_role_with_superuser_needs_the_word_create(self):
        sql = f"CREATE ROLE {ROLE} SUPERUSER"
        data = self.preview(sql)
        self.assertEqual(data["confirm_word"], "CREATE")
        self.assertTrue(any("SUPERUSER" in n["text"] and n["bad"] for n in data["notes"]))
        for wrong in ("create", "Create", "", None, "ALTER"):
            with self.subTest(confirm=wrong), self.assertRaisesRegex(pg.PgError, "Type CREATE in capital letters"):
                self.execute(sql, data["fingerprint"], wrong)
        self.assertFalse(self.role_exists())
        self.execute(sql, data["fingerprint"], "CREATE")
        self.assertTrue(self.role_exists())

    def test_alter_role_shows_the_change_and_rename_needs_the_word(self):
        with admin_connect(self.db) as conn:
            conn.execute(f"CREATE ROLE {ROLE} LOGIN")
        plain = self.preview(f"ALTER ROLE {ROLE} NOLOGIN")
        self.assertEqual((plain["confirm_word"], plain["objects"][0]["text"]), (None, f"~ change role {ROLE} — settings changed"))
        self.execute(f"ALTER ROLE {ROLE} NOLOGIN", plain["fingerprint"])
        self.assertFalse(self.query("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", ROLE)[0][0])
        rename = self.preview(f"ALTER ROLE {ROLE} RENAME TO {ROLE}2")
        self.assertEqual(rename["confirm_word"], "ALTER")
        with self.assertRaisesRegex(pg.PgError, "Type ALTER"):
            self.execute(f"ALTER ROLE {ROLE} RENAME TO {ROLE}2", rename["fingerprint"], "")
        self.assertTrue(self.role_exists())

    def test_drop_role_needs_the_word_drop(self):
        with admin_connect(self.db) as conn:
            conn.execute(f"CREATE ROLE {ROLE}")
        sql = f"DROP ROLE {ROLE}"
        data = self.preview(sql)
        self.assertEqual((data["confirm_word"], data["title"], data["button"]), ("DROP", "DROP ROLE?", "Drop role"))
        for wrong in ("drop", "", None, "DROP "):
            with self.subTest(confirm=wrong), self.assertRaisesRegex(pg.PgError, "Type DROP in capital letters"):
                self.execute(sql, data["fingerprint"], wrong)
        self.assertTrue(self.role_exists())
        result = self.execute(sql, data["fingerprint"], "DROP")
        self.assertFalse(self.role_exists())
        self.assertEqual(AuditEntry.objects.get(pk=result["entry_id"]).payload["kind"], "drop")

    def test_the_changes_must_still_be_what_was_shown(self):
        sql = f"CREATE ROLE {ROLE}"
        data = self.preview(sql)
        tampered = {"changes": [{**data["fingerprint"]["changes"][0], "name": "someone_else"}]}
        with self.assertRaisesRegex(pg.PgError, "changed since you checked"):
            self.execute(sql, tampered)
        with self.assertRaisesRegex(pg.PgError, "changed since you checked"):
            self.execute(sql, {})
        self.assertFalse(self.role_exists())

    def test_a_role_that_already_exists_is_an_error_and_a_noop_has_no_button(self):
        with admin_connect(self.db) as conn:
            conn.execute(f"CREATE ROLE {ROLE}")
        with self.assertRaisesRegex(pg.PgError, "already exists"):
            self.preview(f"CREATE ROLE {ROLE}")
        with self.assertRaisesRegex(pg.PgError, "does not exist"):
            self.preview("DROP ROLE pgdas_t_nobody")
        self.assertIsNone(self.preview("DROP ROLE IF EXISTS pgdas_t_nobody")["button"])


class GrantTests(AdminFixture, TestCase):
    def setUp(self):
        super().setUp()
        with admin_connect(self.db) as conn:
            conn.execute(f"CREATE ROLE {ROLE}")

    def can_select(self):
        return self.query("SELECT has_table_privilege(%s, 'pgdas_w.logged', 'SELECT')", ROLE)[0][0]

    def test_grant_and_revoke_are_previewed_and_recorded(self):
        grant = f"GRANT SELECT ON pgdas_w.logged TO {ROLE}"
        data = self.preview(grant)
        self.assertEqual((data["kind"], data["title"], data["button"], data["confirm_word"]), ("grant", "GRANT?", "Grant", None))
        self.assertIn(f"~ change privileges on table pgdas_w.logged — + {ROLE}=r/", data["objects"][0]["text"])
        self.assertFalse(self.can_select())                                       # only a preview
        result = self.execute(grant, data["fingerprint"])
        self.assertTrue(self.can_select())
        entry = AuditEntry.objects.get(pk=result["entry_id"])
        self.assertEqual((entry.action, entry.record_label, entry.payload["object_type"]), ("grant", "GRANT · 1 change", "PRIVILEGES"))
        revoke = f"REVOKE SELECT ON pgdas_w.logged FROM {ROLE}"
        shown = self.preview(revoke)
        self.assertIn("− ", shown["objects"][0]["text"])
        self.execute(revoke, shown["fingerprint"])
        self.assertFalse(self.can_select())

    def test_granting_a_role_to_a_role_and_a_grant_that_changes_nothing(self):
        with admin_connect(self.db) as conn:
            conn.execute(f"CREATE ROLE {GROUP}")
        data = self.preview(f"GRANT {GROUP} TO {ROLE}")
        self.assertEqual(data["objects"][0]["text"], f"+ create membership {GROUP} -> {ROLE}")
        again = self.preview(f"GRANT SELECT ON pgdas_w.logged TO {ROLE}")
        self.execute(f"GRANT SELECT ON pgdas_w.logged TO {ROLE}", again["fingerprint"])
        none = self.preview(f"GRANT SELECT ON pgdas_w.logged TO {ROLE}")
        self.assertEqual((none["button"], none["count"]), (None, 0))
        with self.assertRaisesRegex(pg.PgError, "nothing to run"):
            self.execute(f"GRANT SELECT ON pgdas_w.logged TO {ROLE}", none["fingerprint"])

    def test_a_grant_on_something_missing_is_explained(self):
        with self.assertRaisesRegex(pg.PgError, "does not exist"):
            self.preview(f"GRANT SELECT ON pgdas_w.nope TO {ROLE}")


class DatabaseTests(AdminFixture, TestCase):
    def exists(self):
        return bool(self.query("SELECT 1 FROM pg_database WHERE datname = %s", DB))

    def test_create_database_is_described_and_then_created(self):
        sql = f"CREATE DATABASE {DB}"
        data = self.preview(sql)
        self.assertEqual((data["title"], data["button"], data["confirm_word"], data["fingerprint"]), ("CREATE DATABASE?", "Create database", None, {"exists": False}))
        self.assertTrue(any("cannot create a database inside a transaction" in n["text"] for n in data["notes"]))
        self.assertFalse(self.exists())
        result = self.execute(sql, data["fingerprint"])
        self.assertTrue(self.exists())
        entry = AuditEntry.objects.get(pk=result["entry_id"])
        self.assertEqual((entry.action, entry.ok, entry.payload["object_type"], entry.record_label), ("create", True, "DATABASE", "CREATE DATABASE · 1 object"))
        with self.assertRaisesRegex(pg.PgError, "already exists"):
            self.preview(sql)

    def test_a_failing_create_database_marks_the_record_as_failed(self):
        sql = f"CREATE DATABASE {DB} OWNER pgdas_t_no_such_role"
        data = self.preview(sql)
        with self.assertRaisesRegex(pg.PgError, "does not exist"):
            self.execute(sql, data["fingerprint"])
        entry = AuditEntry.objects.get()
        self.assertFalse(entry.ok)
        self.assertIn("(failed:", entry.detail)
        self.assertFalse(self.exists())

    def test_drop_database_shows_its_size_and_needs_its_name(self):
        with admin_connect("postgres") as conn:
            conn.execute(f"CREATE DATABASE {DB}")
        sql = f"DROP DATABASE {DB}"
        data = self.preview(sql)
        self.assertEqual((data["title"], data["button"], data["confirm_word"], data["fingerprint"]), ("DROP DATABASE?", "Drop database", DB, {"exists": True}))
        self.assertTrue(any("is lost" in n["text"] and n["bad"] for n in data["notes"]))
        for wrong in ("drop", "DROP", "", None):
            with self.subTest(confirm=wrong), self.assertRaisesRegex(pg.PgError, "Type the database name exactly"):
                self.execute(sql, data["fingerprint"], wrong)
        self.assertTrue(self.exists())
        self.assertFalse(AuditEntry.objects.exists())
        result = self.execute(sql, data["fingerprint"], DB)
        self.assertFalse(self.exists())
        payload = AuditEntry.objects.get(pk=result["entry_id"]).payload
        self.assertEqual((payload["kind"], payload["object_type"]), ("drop", "DATABASE"))
        self.assertIn("size", payload)

    def test_a_database_with_other_sessions_needs_force_and_says_so(self):
        with admin_connect("postgres") as conn:
            conn.execute(f"CREATE DATABASE {DB}")
        with admin_connect(DB):                                                          # someone else is connected
            plain = f"DROP DATABASE {DB}"
            data = self.preview(plain)
            self.assertTrue(any("connection" in n["text"] and n["bad"] for n in data["notes"]))
            with self.assertRaisesRegex(pg.PgError, r'being accessed by other users.*Disconnect the other sessions.*WITH \(FORCE\)'):
                self.execute(plain, data["fingerprint"], DB)
            self.assertTrue(self.exists())
            self.assertFalse(AuditEntry.objects.get().ok)                                # the attempt is recorded as failed
            forced = f"DROP DATABASE {DB} WITH (FORCE)"
            data = self.preview(forced)
            self.assertEqual((data["confirm_word"], data["button"]), (DB, "Drop database"))
            self.execute(forced, data["fingerprint"], DB)
            self.assertFalse(self.exists())

    def test_the_list_counts_other_sessions(self):
        with admin_connect("postgres") as conn:
            conn.execute(f"CREATE DATABASE {DB}")
        with pg.connect(self.server) as conn:
            self.assertEqual({d["name"]: d["sessions"] for d in pg.list_databases(conn)}.get(DB), 0)
        with admin_connect(DB):
            with pg.connect(self.server) as conn:
                self.assertEqual({d["name"]: d["sessions"] for d in pg.list_databases(conn)}[DB], 1)

    def test_drop_of_a_missing_database(self):
        with self.assertRaisesRegex(pg.PgError, "does not exist"):
            self.preview(f"DROP DATABASE {DB}")
        self.assertIsNone(self.preview(f"DROP DATABASE IF EXISTS {DB}")["button"])

    def test_alter_database_is_checked_in_a_rolled_back_transaction(self):
        with admin_connect("postgres") as conn:
            conn.execute(f"CREATE DATABASE {DB}")
        data = self.preview(f"ALTER DATABASE {DB} CONNECTION LIMIT 7")
        self.assertEqual((data["confirm_word"], data["objects"][0]["text"]), (None, f"~ change database {DB} — settings changed"))
        self.assertEqual(self.query("SELECT datconnlimit FROM pg_database WHERE datname = %s", DB)[0][0], -1)
        self.assertEqual(self.preview(f"ALTER DATABASE {DB} RENAME TO {DB}x")["confirm_word"], "ALTER")


class SwitchTests(AdminFixture, TestCase):
    def post(self, name, sql):
        self.client.force_login(self.staff)
        return self.client.post(reverse(name, args=[self.server.pk]), json.dumps({"sql": sql, "db": self.db}), content_type="application/json")

    def test_only_staff_can_use_these_statements(self):
        from django.contrib.auth import get_user_model
        plain = self.client_class()
        plain.force_login(get_user_model().objects.create_user("pat", password="pw-12345-abc"))
        for sql in (f"CREATE ROLE {ROLE}", f"GRANT SELECT ON pgdas_w.logged TO {ROLE}", f"CREATE DATABASE {DB}"):
            with self.subTest(sql=sql):
                response = plain.post(reverse("write_preview", args=[self.server.pk]), json.dumps({"sql": sql, "db": self.db}),
                                      content_type="application/json")
                self.assertIn(response.status_code, (403, 404))                  # not their server: nothing was run
        self.assertFalse(self.role_exists())

    def test_the_endpoint_previews_and_executes(self):
        preview = self.post("write_preview", f"CREATE ROLE {ROLE}")
        self.assertEqual(preview.status_code, 200)
        self.client.force_login(self.staff)
        body = {"sql": f"CREATE ROLE {ROLE}", "db": self.db, "fingerprint": preview.json()["fingerprint"]}
        done = self.client.post(reverse("write_execute", args=[self.server.pk]), json.dumps(body), content_type="application/json")
        self.assertEqual((done.status_code, done.json()["kind"]), (200, "create"))
        self.assertTrue(self.role_exists())


class DialogTests(AdminFixture, TestCase):
    """Create database / Create role / Rename / Delete: the buttons, the dialogs and the scripts behind them."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)

    def database_list(self, client=None):
        return (client or self.client).get(reverse("detail"), {"type": "databases", "server": self.server.pk})

    def other_user(self):
        from django.contrib.auth import get_user_model
        plain = self.client_class()
        plain.force_login(get_user_model().objects.create_user("pat", password="pw-12345-abc"))
        return plain

    def test_the_databases_list_has_create_rename_delete_and_details(self):
        page = self.database_list()
        for text in ('data-admin-dialog="create-db"', "+ Create database", 'data-admin-dialog="rename-db"', 'data-admin-dialog="drop-db"',
                     "data-open-database=", "Details", "Rename", "Delete", 'data-sessions="'):
            self.assertContains(page, text)
        self.assertContains(page, f'data-db="{TARGET["NAME"]}"')
        self.assertNotContains(page, "Review CREATE DATABASE")                       # the old inline form is gone
        data = json.loads(page.content.decode().split('data-admin="')[1].split('"')[0].replace("&quot;", '"'))
        self.assertEqual(data["server"], self.server.pk)
        self.assertIn(TARGET["USER"], data["roles"])
        self.assertIn(TARGET["NAME"], data["databases"])

    def test_other_users_only_get_details(self):
        page = self.database_list(self.other_user())               # a person with no role on this server is not shown it at all
        self.assertEqual(page.status_code, 404)
        for text in ("data-open-database=", "Create database", "Rename", "Delete", "data-admin"):
            self.assertNotContains(page, text, status_code=404)

    def test_the_roles_list_has_create_role(self):
        page = self.client.get(reverse("detail"), {"type": "roles", "server": self.server.pk})
        self.assertContains(page, 'data-admin-dialog="create-role"')
        self.assertContains(page, "+ Create login / group role")
        data = json.loads(page.content.decode().split('data-admin="')[1].split('"')[0].replace("&quot;", '"'))
        self.assertIn(TARGET["USER"], data["roles"])
        tablespaces = self.client.get(reverse("detail"), {"type": "tablespaces", "server": self.server.pk})
        self.assertNotContains(tablespaces, "Create login")

    def test_the_dialogs_and_the_review_popup_are_on_every_page_for_staff_only(self):
        for name in ("server_list", "audit_list"):
            page = self.client.get(reverse(name))
            for text in ('id="dlgCreateDb"', 'id="dlgCreateRole"', 'id="dlgRenameDb"', 'id="dlgDropDb"', "Disconnect the other sessions first", 'id="changeModal"', "admin_dialogs.js", "review.js",
                         'data-tab="privileges"', 'data-tab="membership"', 'data-tab="sql"', "Can login?", "Superuser?"):
                self.assertContains(page, text)
        page = self.client.get(reverse("sql_runner", args=[self.server.pk]))
        self.assertContains(page, 'id="changeModal"')
        plain = self.other_user()
        for name in ("server_list",):
            page = plain.get(reverse(name))
            for text in ("dlgCreateDb", "admin_dialogs.js"):                  # the admin dialogs are for Super Users only ...
                self.assertNotContains(page, text)
            for text in ("changeModal", "review.js"):                         # ... the review popup is on every page: Editors use it for their own changes
                self.assertContains(page, text)

    def test_the_scripts_build_quoted_sql_and_go_through_the_review_popup(self):
        script = open("static/admin_dialogs.js", encoding="utf-8").read()
        for needle in ("pgdasReview.start", "CREATE DATABASE", "CREATE ROLE", "RENAME TO", "DROP DATABASE", "function ident", "function literal",
                       "'********'", "pgdas:changed", "cannot be empty", "textContent"):
            self.assertIn(needle, script)
        self.assertNotIn(".innerHTML = r", script)                                    # values go in as text, never as HTML
        review = open("static/review.js", encoding="utf-8").read()
        for needle in ("pgdasReview", "preview.confirm_word", "confirmInput.value !== checked.word", "onSuccess"):
            self.assertIn(needle, review)
        explorer = open("static/explorer.js", encoding="utf-8").read()
        self.assertIn("data-open-database", explorer)
        self.assertIn("pgdas:changed", explorer)

    def test_the_statements_the_dialogs_send_are_understood_by_the_server(self):
        post = lambda sql: self.client.post(reverse("write_preview", args=[self.server.pk]), json.dumps({"sql": sql, "db": ""}),
                                            content_type="application/json").json()
        created = post(f'CREATE DATABASE "{DB}"\n    WITH OWNER = "{TARGET["USER"]}"\n         TEMPLATE = "template0"\n         ENCODING = \'UTF8\'\n         CONNECTION LIMIT = 5')
        self.assertEqual((created["ok"], created["title"], created["button"]), (True, "CREATE DATABASE?", "Create database"))
        role = post(f'CREATE ROLE "{ROLE}" WITH\n    LOGIN\n    NOSUPERUSER\n    CREATEDB\n    NOCREATEROLE\n    INHERIT\n    NOREPLICATION\n    NOBYPASSRLS\n'
                    f'    CONNECTION LIMIT 3\n    PASSWORD \'pw-1\'\n    VALID UNTIL \'2030-01-01 10:00\'')
        self.assertEqual((role["ok"], role["title"], role["confirm_word"]), (True, "CREATE ROLE?", None))
        self.assertNotIn("pw-1", json.dumps(role))
        with admin_connect("postgres") as conn:
            conn.execute(f'CREATE DATABASE "{DB}"')
        renamed = post(f'ALTER DATABASE "{DB}" RENAME TO "{DB}2"')
        self.assertEqual((renamed["ok"], renamed["confirm_word"]), (True, "ALTER"))
        dropped = post(f'DROP DATABASE "{DB}"')
        self.assertEqual((dropped["ok"], dropped["confirm_word"], dropped["button"]), (True, DB, "Drop database"))

    def test_the_old_prefill_route_is_gone(self):
        page = self.client.get(reverse("sql_runner", args=[self.server.pk]), {"new_database": DB, "db": self.db})
        self.assertEqual(page.context["sql"], "")
        self.assertNotContains(page, "data-autorun")

class LimitedLoginTests(AdminFixture, TestCase):
    """The saved login is a PostgreSQL role with limited rights: what it may do is allowed, anything else is refused with
    PostgreSQL's own reason, before any popup asks for a typed word."""

    LIMITED = ROLE + "2"

    def limited_server(self, attributes=""):
        from servers.models import ServerConnection
        with admin_connect(self.db) as conn:
            conn.execute(f"CREATE ROLE {self.LIMITED} LOGIN PASSWORD 'lim-pw-1' {attributes}")
            conn.execute(f"GRANT USAGE ON SCHEMA pgdas_w TO {self.LIMITED}")
            conn.execute(f"GRANT SELECT ON pgdas_w.logged TO {self.LIMITED}")
        server = ServerConnection(name="limited", host=TARGET["HOST"], port=int(TARGET["PORT"]), database=TARGET["NAME"],
                                  username=self.LIMITED, sslmode="prefer")
        server.set_password("lim-pw-1")
        server.save()
        return server

    def preview_as(self, server, sql):
        return changes.preview(server, self.db, sql)

    def test_create_database_without_createdb_is_refused_up_front(self):
        server = self.limited_server()
        with self.assertRaisesRegex(pg.PgError, rf'permission denied to create database.*"{self.LIMITED}".*CREATEDB'):
            self.preview_as(server, f"CREATE DATABASE {DB}")
        self.assertFalse(AuditEntry.objects.exists())

    def test_create_database_with_createdb_is_allowed(self):
        server = self.limited_server("CREATEDB")
        data = self.preview_as(server, f"CREATE DATABASE {DB}")
        self.assertEqual(data["button"], "Create database")
        result = changes.execute(server, self.db, f"CREATE DATABASE {DB}", data["fingerprint"], None, self.staff)
        self.assertTrue(self.query("SELECT 1 FROM pg_database WHERE datname = %s", DB))
        self.assertEqual(AuditEntry.objects.get(pk=result["entry_id"]).server_id, server.pk)

    def test_dropping_a_database_it_does_not_own_is_refused_up_front(self):
        server = self.limited_server("CREATEDB")
        with admin_connect("postgres") as conn:
            conn.execute(f"CREATE DATABASE {DB}")                                       # owned by the admin login, not the limited one
        with self.assertRaisesRegex(pg.PgError, rf'must be owner of database {DB}.*"{self.LIMITED}".*not its owner'):
            self.preview_as(server, f"DROP DATABASE {DB}")
        self.assertTrue(self.query("SELECT 1 FROM pg_database WHERE datname = %s", DB))

    def test_a_role_that_cannot_create_roles_gets_postgresqls_reason(self):
        server = self.limited_server()
        with self.assertRaisesRegex(pg.PgError, "permission denied to create role"):
            self.preview_as(server, "CREATE ROLE pgdas_t_nobody")

    def test_row_and_table_changes_without_the_right_are_refused_with_the_reason(self):
        server = self.limited_server()
        for sql, reason in (("DELETE FROM pgdas_w.logged WHERE id = 1", "permission denied for table logged"),
                            ("UPDATE pgdas_w.logged SET what = 'z'", "permission denied for table logged"),
                            ("TRUNCATE pgdas_w.logged", "permission denied for table logged"),
                            ("DROP TABLE pgdas_w.logged", "must be owner of table logged"),
                            ("ALTER TABLE pgdas_w.logged ADD COLUMN c int", "must be owner of table logged"),
                            ("DELETE FROM pgdas_w.ident", "permission denied for table ident")):
            with self.subTest(sql=sql), self.assertRaisesRegex(pg.PgError, reason):
                self.preview_as(server, sql)
        self.assertEqual(self.count("logged"), 2)
        self.assertFalse(AuditEntry.objects.exists())

    def test_what_the_role_may_do_is_allowed(self):
        server = self.limited_server()
        with admin_connect(self.db) as conn:
            conn.execute(f"GRANT UPDATE ON pgdas_w.logged TO {self.LIMITED}")
        data = self.preview_as(server, "UPDATE pgdas_w.logged SET what = 'z' WHERE id = 1")
        self.assertEqual((data["count"], data["button"]), (1, "Update 1 record"))
        changes.execute(server, self.db, "UPDATE pgdas_w.logged SET what = 'z' WHERE id = 1", data["fingerprint"], None, self.staff)
        self.assertEqual(self.query("SELECT what FROM pgdas_w.logged WHERE id = 1")[0][0], "z")

    def test_a_grant_the_role_may_not_make_says_why_it_does_nothing(self):
        server = self.limited_server()
        data = self.preview_as(server, f"GRANT SELECT ON pgdas_w.logged TO {self.LIMITED}")
        self.assertIsNone(data["button"])
        self.assertTrue(any("PostgreSQL says: no privileges were granted" in n["text"] and n["bad"] for n in data["notes"]))


class DatabaseSecurityTests(AdminFixture, TestCase):
    """CREATE DATABASE followed by GRANT statements on it: what the dialog's Security tab sends."""

    def make_role(self):
        with admin_connect("postgres") as conn:
            conn.execute(f"CREATE ROLE {ROLE}")

    def can(self, privilege):
        return self.query("SELECT has_database_privilege(%s, %s, %s)", ROLE, DB, privilege)[0][0]

    def script(self, *grants):
        return ";\n".join([f"CREATE DATABASE {DB}", *grants])

    def test_parse_splits_the_create_from_its_grants(self):
        parsed = server_admin.parse(self.script(f"GRANT CREATE ON DATABASE {DB} TO {ROLE}", f"GRANT CONNECT ON DATABASE {DB} TO PUBLIC WITH GRANT OPTION;"))
        self.assertEqual((parsed["verb"], parsed["name"], parsed["transactional"]), ("CREATE", DB, False))
        self.assertEqual(parsed["statement"], f"CREATE DATABASE {DB}")
        self.assertEqual(len(parsed["grants"]), 2)
        self.assertEqual(sum(line.startswith("GRANT") for line in parsed["shown"].splitlines()), 2)
        self.assertEqual(server_admin._grantees(parsed["grants"][0]), [ROLE])
        self.assertEqual(server_admin._grantees(parsed["grants"][1]), [])                  # PUBLIC is not a role to look up

    def test_only_grants_on_that_database_may_follow(self):
        for text, message in ((self.script("DROP DATABASE postgres"), "only GRANT or REVOKE"),
                              (self.script("SELECT 1"), "only GRANT or REVOKE"),
                              (self.script("GRANT CREATE ON DATABASE postgres TO PUBLIC"), "ON DATABASE " + DB),
                              (self.script("GRANT SELECT ON ALL TABLES IN SCHEMA public TO PUBLIC"), "ON DATABASE " + DB)):
            with self.subTest(text=text), self.assertRaisesRegex(pg.PgError, message):
                server_admin.parse(text)

    def test_preview_lists_the_grants_and_create_runs_them_with_the_database(self):
        self.make_role()
        sql = self.script(f"GRANT CREATE, TEMPORARY ON DATABASE {DB} TO {ROLE}", f"GRANT CONNECT ON DATABASE {DB} TO {ROLE} WITH GRANT OPTION")
        data = self.preview(sql)
        self.assertEqual(data["button"], "Create database")
        self.assertEqual(len(data["objects"]), 3)
        self.assertTrue(any("2 privilege statements" in n["text"] for n in data["notes"]))
        self.assertEqual(self.query("SELECT 1 FROM pg_database WHERE datname = %s", DB), [])
        result = self.execute(sql, data["fingerprint"])
        self.assertIn("and 2 privilege statements run", result["message"])
        self.assertTrue(self.can("CREATE"))
        self.assertTrue(self.can("CONNECT WITH GRANT OPTION"))
        self.assertFalse(self.can("CREATE WITH GRANT OPTION"))
        entry = AuditEntry.objects.get(pk=result["entry_id"])
        self.assertEqual(len(entry.payload["grants"]), 2)
        self.assertIn(f"GRANT CREATE, TEMPORARY ON DATABASE {DB}", entry.detail)

    def test_a_missing_role_is_refused_before_anything_is_created(self):
        with self.assertRaisesRegex(pg.PgError, "do not exist: pgdas_t_no_such_role"):
            self.preview(self.script(f"GRANT CREATE ON DATABASE {DB} TO pgdas_t_no_such_role"))
        self.assertEqual(self.query("SELECT 1 FROM pg_database WHERE datname = %s", DB), [])
        self.assertFalse(AuditEntry.objects.exists())

    def test_a_grant_refused_after_the_create_leaves_the_database_and_says_so(self):
        self.make_role()
        sql = self.script(f"GRANT CREATE ON DATABASE {DB} TO {ROLE}", f"GRANT NOPE ON DATABASE {DB} TO {ROLE}")
        data = self.preview(sql)
        with self.assertRaisesRegex(pg.PgError, "was created, but the privilege statement 2 failed"):
            self.execute(sql, data["fingerprint"])
        self.assertEqual(len(self.query("SELECT 1 FROM pg_database WHERE datname = %s", DB)), 1)
        self.assertTrue(self.can("CREATE"))
        entry = AuditEntry.objects.get()
        self.assertTrue(entry.ok)
        self.assertIn("created, but this privilege statement failed", entry.detail)


class RedactionTests(TestCase):
    def test_record_never_keeps_a_password(self):
        entry = record(None, "sql.run", None, "CREATE ROLE a PASSWORD 'hunter2'")
        self.assertEqual(entry.detail, "CREATE ROLE a PASSWORD '********'")
