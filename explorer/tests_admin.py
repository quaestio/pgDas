"""Databases, users (roles) and grants from the SQL runner: parsing, the popup facts, the confirmation words, no passwords kept."""
import json

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from audit.models import AuditEntry, record, redact_secrets

from . import changes, pg, server_admin
from .tests import admin_connect, TARGET
from .tests_writes import WriteFixture

ROLE, GROUP, DB = "pgdeck_t_user", "pgdeck_t_group", "pgdeck_t_db"


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
            "DROP DATABASE IF EXISTS shop": ("DROP", "DATABASE", "shop", False, "DROP"),
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
        for text, message in (("CREATE DATABASE a; CREATE DATABASE b", "one CREATE statement"),
                              ("ALTER DATABASE a SET TABLESPACE t", "tablespace"), ("CREATE DATABASE", "Write it as")):
            with self.subTest(text=text), self.assertRaisesRegex(pg.PgError, message):
                server_admin.parse(text)


class AdminFixture(WriteFixture):
    def setUp(self):
        super().setUp()
        self.clean()
        self.addCleanup(self.clean)

    def clean(self):
        with admin_connect(TARGET["NAME"]) as conn:
            for role in (ROLE, GROUP, ROLE + "2"):
                if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                    conn.execute(f"DROP OWNED BY {role}")
                    conn.execute(f"DROP ROLE {role}")
        with admin_connect("postgres") as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {DB}")

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
            self.preview("DROP ROLE pgdeck_t_nobody")
        self.assertIsNone(self.preview("DROP ROLE IF EXISTS pgdeck_t_nobody")["button"])


class GrantTests(AdminFixture, TestCase):
    def setUp(self):
        super().setUp()
        with admin_connect(self.db) as conn:
            conn.execute(f"CREATE ROLE {ROLE}")

    def can_select(self):
        return self.query("SELECT has_table_privilege(%s, 'pgdeck_w.logged', 'SELECT')", ROLE)[0][0]

    def test_grant_and_revoke_are_previewed_and_recorded(self):
        grant = f"GRANT SELECT ON pgdeck_w.logged TO {ROLE}"
        data = self.preview(grant)
        self.assertEqual((data["kind"], data["title"], data["button"], data["confirm_word"]), ("grant", "GRANT?", "Grant", None))
        self.assertIn(f"~ change privileges on table pgdeck_w.logged — + {ROLE}=r/", data["objects"][0]["text"])
        self.assertFalse(self.can_select())                                       # only a preview
        result = self.execute(grant, data["fingerprint"])
        self.assertTrue(self.can_select())
        entry = AuditEntry.objects.get(pk=result["entry_id"])
        self.assertEqual((entry.action, entry.record_label, entry.payload["object_type"]), ("grant", "GRANT · 1 change", "PRIVILEGES"))
        revoke = f"REVOKE SELECT ON pgdeck_w.logged FROM {ROLE}"
        shown = self.preview(revoke)
        self.assertIn("− ", shown["objects"][0]["text"])
        self.execute(revoke, shown["fingerprint"])
        self.assertFalse(self.can_select())

    def test_granting_a_role_to_a_role_and_a_grant_that_changes_nothing(self):
        with admin_connect(self.db) as conn:
            conn.execute(f"CREATE ROLE {GROUP}")
        data = self.preview(f"GRANT {GROUP} TO {ROLE}")
        self.assertEqual(data["objects"][0]["text"], f"+ create membership {GROUP} -> {ROLE}")
        again = self.preview(f"GRANT SELECT ON pgdeck_w.logged TO {ROLE}")
        self.execute(f"GRANT SELECT ON pgdeck_w.logged TO {ROLE}", again["fingerprint"])
        none = self.preview(f"GRANT SELECT ON pgdeck_w.logged TO {ROLE}")
        self.assertEqual((none["button"], none["count"]), (None, 0))
        with self.assertRaisesRegex(pg.PgError, "nothing to run"):
            self.execute(f"GRANT SELECT ON pgdeck_w.logged TO {ROLE}", none["fingerprint"])

    def test_a_grant_on_something_missing_is_explained(self):
        with self.assertRaisesRegex(pg.PgError, "does not exist"):
            self.preview(f"GRANT SELECT ON pgdeck_w.nope TO {ROLE}")


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
        sql = f"CREATE DATABASE {DB} OWNER pgdeck_t_no_such_role"
        data = self.preview(sql)
        with self.assertRaisesRegex(pg.PgError, "does not exist"):
            self.execute(sql, data["fingerprint"])
        entry = AuditEntry.objects.get()
        self.assertFalse(entry.ok)
        self.assertIn("(failed:", entry.detail)
        self.assertFalse(self.exists())

    def test_drop_database_shows_its_size_and_needs_the_word_drop(self):
        with admin_connect("postgres") as conn:
            conn.execute(f"CREATE DATABASE {DB}")
        sql = f"DROP DATABASE {DB}"
        data = self.preview(sql)
        self.assertEqual((data["title"], data["button"], data["confirm_word"], data["fingerprint"]), ("DROP DATABASE?", "Drop database", "DROP", {"exists": True}))
        self.assertTrue(any("is lost" in n["text"] and n["bad"] for n in data["notes"]))
        for wrong in ("drop", "", None):
            with self.subTest(confirm=wrong), self.assertRaisesRegex(pg.PgError, "Type DROP in capital letters"):
                self.execute(sql, data["fingerprint"], wrong)
        self.assertTrue(self.exists())
        self.assertFalse(AuditEntry.objects.exists())
        result = self.execute(sql, data["fingerprint"], "DROP")
        self.assertFalse(self.exists())
        payload = AuditEntry.objects.get(pk=result["entry_id"]).payload
        self.assertEqual((payload["kind"], payload["object_type"]), ("drop", "DATABASE"))
        self.assertIn("size", payload)

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
        for sql in (f"CREATE ROLE {ROLE}", f"GRANT SELECT ON pgdeck_w.logged TO {ROLE}", f"CREATE DATABASE {DB}"):
            with self.subTest(sql=sql):
                response = plain.post(reverse("write_preview", args=[self.server.pk]), json.dumps({"sql": sql, "db": self.db}),
                                      content_type="application/json")
                self.assertEqual(response.status_code, 403)
                self.assertIn("Only staff", response.json()["error"])
        self.assertFalse(self.role_exists())

    def test_the_endpoint_previews_and_executes(self):
        preview = self.post("write_preview", f"CREATE ROLE {ROLE}")
        self.assertEqual(preview.status_code, 200)
        self.client.force_login(self.staff)
        body = {"sql": f"CREATE ROLE {ROLE}", "db": self.db, "fingerprint": preview.json()["fingerprint"]}
        done = self.client.post(reverse("write_execute", args=[self.server.pk]), json.dumps(body), content_type="application/json")
        self.assertEqual((done.status_code, done.json()["kind"]), (200, "create"))
        self.assertTrue(self.role_exists())


class CreateDatabaseFormTests(AdminFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)

    def database_list(self):
        return self.client.get(reverse("detail"), {"type": "databases", "server": self.server.pk})

    def test_the_list_offers_create_database_to_staff(self):
        page = self.database_list()
        self.assertContains(page, "Review CREATE DATABASE")
        self.assertContains(page, 'name="new_database"')
        self.assertContains(page, "<option>UTF8</option>")
        self.assertContains(page, f'<option value="{TARGET["USER"]}">')                       # owners come from the server's roles

    def test_it_is_absent_for_other_users(self):
        from django.contrib.auth import get_user_model
        plain = self.client_class()
        plain.force_login(get_user_model().objects.create_user("pat", password="pw-12345-abc"))
        self.assertNotContains(plain.get(reverse("detail"), {"type": "databases", "server": self.server.pk}), "Review CREATE DATABASE")

    def test_the_form_fills_in_the_sql_page_and_opens_the_review_popup(self):
        url = reverse("sql_runner", args=[self.server.pk])
        page = self.client.get(url, {"new_database": DB, "owner": ROLE, "encoding": "UTF8", "db": self.db})
        self.assertEqual(page.context["sql"], f'CREATE DATABASE "{DB}" OWNER "{ROLE}" ENCODING \'UTF8\' TEMPLATE template0')
        self.assertContains(page, 'data-autorun="1"')
        self.assertFalse(self.query("SELECT 1 FROM pg_database WHERE datname = %s", DB))     # filled in, not run
        minimal = self.client.get(url, {"new_database": DB, "db": self.db})
        self.assertEqual(minimal.context["sql"], f'CREATE DATABASE "{DB}"')

    def test_a_hostile_name_is_quoted_and_nothing_else_is_accepted(self):
        url = reverse("sql_runner", args=[self.server.pk])
        page = self.client.get(url, {"new_database": 'x"; DROP DATABASE y; --', "encoding": "'; DROP", "db": self.db})
        self.assertEqual(page.context["sql"], 'CREATE DATABASE "x""; DROP DATABASE y; --"')     # one quoted name, no encoding
        plain = self.client_class()
        from django.contrib.auth import get_user_model
        plain.force_login(get_user_model().objects.create_user("pat", password="pw-12345-abc"))
        self.assertNotContains(plain.get(url, {"new_database": DB, "db": self.db}), 'data-autorun="1"')       # only staff get the popup


class RedactionTests(TestCase):
    def test_record_never_keeps_a_password(self):
        entry = record(None, "sql.run", None, "CREATE ROLE a PASSWORD 'hunter2'")
        self.assertEqual(entry.detail, "CREATE ROLE a PASSWORD '********'")
