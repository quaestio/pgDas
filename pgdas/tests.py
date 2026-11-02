"""PGDAS's own storage is one SQLite file: no database server is needed to install or run it."""
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

BASE_DIR = Path(settings.BASE_DIR)


class OwnStorageIsSqliteTests(SimpleTestCase):
    def test_settings_use_a_sqlite_file_tuned_for_a_small_team(self):
        config = settings.DATABASES["default"]
        self.assertEqual(config["ENGINE"], "django.db.backends.sqlite3")
        # (While tests run Django swaps NAME for an in-memory database, so check the configured path.)
        self.assertTrue(str(settings.SQLITE_PATH).endswith(".sqlite3"))
        options = config["OPTIONS"]
        self.assertEqual(options["init_command"], "PRAGMA journal_mode=WAL;")   # readers and a writer share the file
        self.assertEqual(options["transaction_mode"], "IMMEDIATE")              # a writer waits its turn, no "locked" errors
        self.assertGreaterEqual(options["timeout"], 10)

    def test_the_fast_password_hasher_is_only_used_while_testing(self):
        self.assertEqual(settings.PASSWORD_HASHERS, ["django.contrib.auth.hashers.MD5PasswordHasher"])   # this test run
        # Any other command (runserver, createsuperuser, shell, ...) must keep Django's strong default hashing.
        env = dict(os.environ, PGDAS_DEBUG="1")
        result = subprocess.run([sys.executable, str(BASE_DIR / "manage.py"), "shell", "-c",
                                 "from django.conf import settings; print(settings.PASSWORD_HASHERS[0])"],
                                cwd=BASE_DIR, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-600:])
        # (`shell` prints an "objects imported automatically" banner first, so look at the last line.)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "django.contrib.auth.hashers.PBKDF2PasswordHasher")

    def test_the_data_folder_and_files_are_not_committed(self):
        ignored = (BASE_DIR / ".gitignore").read_text(encoding="utf-8")
        for pattern in ("data/", "*.sqlite3", "*.sqlite3-wal"):
            self.assertIn(pattern, ignored)

    def test_install_files_describe_sqlite_not_a_database_server(self):
        compose = (BASE_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn("postgres:", compose)                  # no database container to run
        self.assertIn("pgdas-data:/app/data", compose)         # the SQLite file is kept in a volume
        self.assertIn("PGDAS_SQLITE_PATH", compose)
        dockerfile = (BASE_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("VOLUME", dockerfile)
        # The image must never contain the local .env, the SQLite data or the virtualenv.
        ignore = (BASE_DIR / ".dockerignore").read_text(encoding="utf-8").split()
        for entry in (".env", "data", ".venv"):
            self.assertIn(entry, ignore)
        example = (BASE_DIR / ".env.example").read_text(encoding="utf-8")
        self.assertNotIn("PGDAS_DB_PASSWORD", example.replace("PGDAS_TEST_PG_PASSWORD", ""))
        self.assertIn("PGDAS_SQLITE_PATH", example)
        readme = (BASE_DIR / "README.md").read_text(encoding="utf-8")
        self.assertIn("no database server of its own", readme)
        self.assertNotIn("Create the settings database first", readme)

    def test_a_fresh_install_needs_no_database_server_settings(self):
        """Run migrate in a clean process with no PGDAS_DB_* settings and a temporary SQLite path."""
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "nested" / "pgdas.sqlite3"
            env = {key: value for key, value in os.environ.items() if not key.startswith(("PGDAS_DB_", "PGDAS_TEST_"))}
            env.update({"PGDAS_DEBUG": "1", "PGDAS_SQLITE_PATH": str(target)})
            result = subprocess.run([sys.executable, str(BASE_DIR / "manage.py"), "migrate", "--noinput"],
                                    cwd=BASE_DIR, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr[-800:])
            self.assertTrue(target.exists())                                    # the missing folder was created too
            with closing(sqlite3.connect(target)) as db:     # (sqlite3's own `with` does not close the file)
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                mode = db.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertTrue({"auth_user", "servers_serverconnection", "audit_auditentry", "django_session"} <= tables)
            self.assertEqual(mode, "wal")                                       # persisted in the file by init_command


class BrandingTests(TestCase):
    """The product is called PGDAS (PostgreSQL Database Access System) and uses its logo and icons."""

    def test_icons_and_logos_are_shipped(self):
        for name in ("favicon.ico", "pgdas-32.png", "pgdas-64.png", "pgdas-180.png", "pgdas-192.png", "pgdas-400.png", "pgdas-800.png"):
            self.assertTrue((BASE_DIR / "static" / "img" / name).is_file(), name)

    def test_the_sign_in_page_shows_the_logo_and_the_icons_are_linked(self):
        page = self.client.get("/login/")
        for text in ("img/pgdas-400.png", "img/pgdas-800.png", "PGDAS — PostgreSQL Database Access System", "img/favicon.ico", "apple-touch-icon",
                     "<title>Sign in · PGDAS</title>"):
            self.assertContains(page, text)
        self.assertNotContains(page, "PgDeck")
        self.assertNotContains(page, "DECK")

    def test_the_app_shows_the_icon_and_the_new_name_when_signed_in(self):
        user = get_user_model().objects.create_user("pat", password="pw-12345-abc", is_staff=True)
        self.client.force_login(user)
        page = self.client.get("/")
        self.assertContains(page, "brand-icon")
        self.assertContains(page, "PG<span>DAS</span>")
        self.assertContains(page, "· PGDAS</title>")
        self.assertNotContains(page, "PgDeck")

    def test_no_visible_template_text_still_says_the_old_name(self):
        for path in (BASE_DIR / "templates").rglob("*.html"):
            self.assertNotIn("PgDeck", path.read_text(encoding="utf-8"), path.name)

class MenuRailTests(TestCase):
    """The menu is a slim icon rail down the left edge, in front of the sidebar, so the Object Browser keeps the whole sidebar."""

    def page(self, staff, path="/"):
        user = get_user_model().objects.create_user("staff" if staff else "viewer", password="pw-12345-abc", is_staff=staff)
        self.client.force_login(user)
        html = self.client.get(path).content.decode()
        return html, html[html.index('<nav class="rail"'):html.index("</nav>", html.index('<nav class="rail"'))]

    def test_staff_rail_has_an_icon_for_every_page_and_comes_before_the_sidebar(self):
        html, rail = self.page(True)
        for label in ("Object Browser", "Audit log", "Backups", "Monitor", "Alerts &amp; trends"):
            self.assertIn(f'data-label="{label}" aria-label="{label}"', rail)
        self.assertEqual(rail.count("<svg"), rail.count("<a "))                           # an icon for every link, no text in the rail itself
        self.assertGreaterEqual(rail.count("<a "), 5)                                      # (more links may be added to the rail later)
        self.assertLess(html.index('<nav class="rail"'), html.index("<aside"))
        self.assertIn('<div class="app">\n<nav class="rail"', html)
        side = html[html.index("<aside"):html.index("</aside>")]
        self.assertNotIn("menu-group", side)                                               # nothing of the old group is left in the sidebar
        self.assertNotIn("ADMINISTRATION", html)
        self.assertGreater(side.index('id="tree"'), side.index("OBJECT BROWSER"))

    def test_the_page_you_are_on_is_marked(self):
        _, rail = self.page(True, "/")
        self.assertEqual(rail.count(" active"), 1)
        self.assertIn('class="rail-item active" href="/" data-label="Object Browser"', rail)
        self.assertIn('aria-current="page"', rail)
        for path, label in (("/audit/", "Audit log"), ("/backups/", "Backups"), ("/monitor/", "Monitor"), ("/watch/", "Alerts &amp; trends")):
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                rail = html[html.index('<nav class="rail"'):html.index("</nav>", html.index('<nav class="rail"'))]
                self.assertEqual(rail.count(" active"), 1)
                self.assertRegex(rail, rf'class="rail-item active" href="{path}" data-label="{label}"')

    def test_people_who_are_not_staff_only_get_the_object_browser_icon(self):
        _, rail = self.page(False)
        self.assertEqual(rail.count("<a "), 1)
        self.assertIn('data-label="Object Browser"', rail)
        for label in ("Audit log", "Backups", "Monitor", "Alerts"):
            self.assertNotIn(label, rail)

    def test_the_stylesheet_makes_a_narrow_sticky_column_with_a_bar_on_the_active_icon(self):
        css = (BASE_DIR / "static" / "sidebar.css").read_text(encoding="utf-8")
        for needle in (".rail {", "position: sticky", "width: 52px", ".rail-item.active", "inset 3px 0 0 var(--accent)",
                       "content: attr(data-label)", ".rail-item:focus-visible::after"):
            self.assertIn(needle, css)
        script = (BASE_DIR / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("adminMenu", script)                                              # the fold-away button of the old group is gone
        self.assertIn('href="/static/sidebar.css"', self.client.get("/login/").content.decode())
