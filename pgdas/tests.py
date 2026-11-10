"""PGDAS's own storage: a PostgreSQL database (default) or one SQLite file, chosen with PGDAS_APP_DB_ENGINE. The managed servers are separate."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.checks import run_checks
from django.test import override_settings

from pgdas.testing import TestCase

BASE_DIR = Path(settings.BASE_DIR)


def manage(*args, **env):
    """Run manage.py in a clean process with the given environment on top of this one's. Returns the CompletedProcess."""
    merged = dict(os.environ, PGDAS_DEBUG="1")
    merged.update(env)
    return subprocess.run([sys.executable, str(BASE_DIR / "manage.py"), *args], cwd=BASE_DIR, env=merged, capture_output=True, text=True, timeout=180)


def settings_seen_by_a_new_process(**env):
    code = ("import json; from django.conf import settings; d = settings.DATABASES['default']; "
            "print(json.dumps({'engine': d['ENGINE'], 'options': d.get('OPTIONS'), 'name': str(d['NAME']), 'chosen': settings.APP_DB_ENGINE}))")
    result = manage("shell", "-c", code, **env)
    return result, (json.loads(result.stdout.strip().splitlines()[-1]) if result.returncode == 0 else None)


class OwnStorageTests(TestCase):          # (a TestCase: running the system checks reads the database)
    def test_this_run_uses_the_chosen_engine_with_its_settings(self):
        config = settings.DATABASES["default"]
        if settings.APP_DB_ENGINE == "sqlite":
            self.assertEqual(config["ENGINE"], "django.db.backends.sqlite3")
            options = config["OPTIONS"]
            self.assertEqual(options["init_command"], "PRAGMA journal_mode=WAL;")      # readers and a writer share the file
            self.assertEqual(options["transaction_mode"], "IMMEDIATE")                 # a writer waits its turn, no instant "locked" errors
            self.assertGreaterEqual(options["timeout"], 10)
        else:
            self.assertEqual(config["ENGINE"], "django.db.backends.postgresql")
            for key in ("NAME", "USER", "HOST", "PORT"):
                self.assertTrue(config[key], key)
            self.assertEqual(config["CONN_MAX_AGE"], 60)                               # connections are reused, not opened per request

    def test_postgresql_is_the_default_and_sqlite_needs_no_database_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "nested" / "pgdas.sqlite3"
            result, seen = settings_seen_by_a_new_process(PGDAS_APP_DB_ENGINE="")
            self.assertEqual(result.returncode, 0, result.stderr[-500:])
            self.assertEqual((seen["chosen"], seen["engine"]), ("postgresql", "django.db.backends.postgresql"))
            result, seen = settings_seen_by_a_new_process(PGDAS_APP_DB_ENGINE=" SQLite ", PGDAS_SQLITE_PATH=str(target),
                                                          PGDAS_APP_DB_NAME="", PGDAS_APP_DB_USER="")      # nothing of PostgreSQL is needed
            self.assertEqual(result.returncode, 0, result.stderr[-500:])
            self.assertEqual((seen["chosen"], seen["engine"], seen["name"]), ("sqlite", "django.db.backends.sqlite3", str(target)))
            self.assertEqual((seen["options"]["init_command"], seen["options"]["transaction_mode"]), ("PRAGMA journal_mode=WAL;", "IMMEDIATE"))
            self.assertTrue(target.parent.is_dir())                                    # the missing folder was created

    def test_an_unknown_engine_stops_the_start_with_a_clear_message(self):
        result = manage("check", PGDAS_APP_DB_ENGINE="mysql")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PGDAS_APP_DB_ENGINE must be postgresql or sqlite", result.stderr)

    def test_a_missing_postgresql_setting_stops_the_start_with_a_clear_message(self):
        result = manage("check", PGDAS_APP_DB_ENGINE="postgresql", PGDAS_APP_DB_NAME="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Set PGDAS_APP_DB_NAME", result.stderr)
        self.assertIn("PostgreSQL database", result.stderr)

    def test_every_migration_runs_on_a_fresh_sqlite_file(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "fresh.sqlite3"
            result = manage("migrate", "--noinput", PGDAS_APP_DB_ENGINE="sqlite", PGDAS_SQLITE_PATH=str(target))
            self.assertEqual(result.returncode, 0, result.stderr[-800:])
            with closing(sqlite3.connect(target)) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
                mode = db.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertTrue({"auth_user", "servers_serverconnection", "audit_auditentry", "django_session", "access_control_organization"} <= tables)
            self.assertIn("accounts_user_email_unique_ci", indexes)                    # the case-insensitive unique email rule exists there too
            self.assertEqual(mode, "wal")

    def test_the_fast_password_hasher_is_only_used_while_testing(self):
        self.assertEqual(settings.PASSWORD_HASHERS, ["django.contrib.auth.hashers.MD5PasswordHasher"])   # this test run
        # Any other command (runserver, createsuperuser, shell, ...) must keep Django's strong default hashing.
        result = manage("shell", "-c", "from django.conf import settings; print(settings.PASSWORD_HASHERS[0])")
        self.assertEqual(result.returncode, 0, result.stderr[-600:])
        # (`shell` prints an "objects imported automatically" banner first, so look at the last line.)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "django.contrib.auth.hashers.PBKDF2PasswordHasher")

    def test_the_data_folder_and_sqlite_files_are_not_committed(self):
        ignored = (BASE_DIR / ".gitignore").read_text(encoding="utf-8")
        for pattern in ("data/", "*.sqlite3", "*.sqlite3-wal", "*.sqlite3-shm"):
            self.assertIn(pattern, ignored)

    def test_install_files_describe_both_engines(self):
        compose = (BASE_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("postgres:", compose)                     # PGDAS's own database can run next to it ...
        self.assertIn('profiles: ["postgres"]', compose)        # ... and is left out when SQLite is chosen
        self.assertIn("PGDAS_APP_DB_ENGINE", compose)
        self.assertIn("PGDAS_APP_DB_HOST: db", compose)
        self.assertIn("pgdas-db:/var/lib/postgresql", compose)
        dockerfile = (BASE_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("PGDAS_SQLITE_PATH", dockerfile)
        # The image must never contain the local .env, the data folder or the virtualenv.
        ignore = (BASE_DIR / ".dockerignore").read_text(encoding="utf-8").split()
        for entry in (".env", "data", ".venv"):
            self.assertIn(entry, ignore)
        example = (BASE_DIR / ".env.example").read_text(encoding="utf-8")
        for key in ("PGDAS_APP_DB_ENGINE", "PGDAS_SQLITE_PATH", "PGDAS_APP_DB_NAME", "PGDAS_APP_DB_USER", "PGDAS_APP_DB_PASSWORD", "PGDAS_APP_DB_HOST"):
            self.assertIn(key, example)
        self.assertNotIn("PGDAS_DB_PASSWORD", example.replace("PGDAS_TEST_PG_PASSWORD", "").replace("PGDAS_APP_DB_PASSWORD", ""))
        readme = (BASE_DIR / "README.md").read_text(encoding="utf-8")
        for needle in ("PGDAS_APP_DB_ENGINE", "PGDAS_APP_DB_NAME", "SQLite"):
            self.assertIn(needle, readme)

    @override_settings(APP_DB_ENGINE="sqlite", DEBUG=False)
    def test_a_sqlite_install_that_is_not_in_debug_gets_a_warning_a_postgresql_one_does_not(self):
        warnings = [w for w in run_checks() if w.id == "access_control.W002"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("one writer at a time", warnings[0].hint)
        with override_settings(APP_DB_ENGINE="postgresql"):
            self.assertEqual([w for w in run_checks() if w.id == "access_control.W002"], [])
        with override_settings(DEBUG=True):
            self.assertEqual([w for w in run_checks() if w.id == "access_control.W002"], [])


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
        self.assertEqual(rail.count("<svg"), rail.count('class="rail-item'))              # an icon for every link, no text in the rail itself
        self.assertGreaterEqual(rail.count('class="rail-item'), 5)                         # (more links may be added to the rail later)
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


class RailLogoTests(TestCase):
    """The logo stays available when the sidebar is collapsed: the rail carries it, with an entrance animation."""

    def test_the_rail_starts_with_the_logo_which_goes_home(self):
        user = get_user_model().objects.create_user("logo", password="pw-12345-abc", is_staff=True)
        self.client.force_login(user)
        html = self.client.get("/").content.decode()
        rail = html[html.index('<nav class="rail"'):html.index("</nav>", html.index('<nav class="rail"'))]
        self.assertIn('<a class="rail-logo" href="/" aria-label="PGDAS home"', rail)
        self.assertIn("img/pgdas-64.png", rail[:rail.index('class="rail-item')])             # the logo comes before the menu icons
        self.assertIn('class="brand"', html[html.index("<aside"):html.index("</aside>")])     # the sidebar keeps its own wordmark

    def test_it_shows_when_the_sidebar_is_collapsed_and_animates_in(self):
        css = (BASE_DIR / "static" / "sidebar.css").read_text(encoding="utf-8")
        for needle in (".rail-logo { display: none;", "html.sidebar-collapsed .rail-logo { display: grid; animation: logo-in",
                       "@keyframes logo-in", "@keyframes logo-glow", "@media (max-width: 800px) { .rail-logo { display: grid;"):
            self.assertIn(needle, css)
        self.assertIn("(min-width: 801px)", css[css.index("html.sidebar-collapsed .rail-logo") - 60:css.index("html.sidebar-collapsed .rail-logo")])

    def test_people_who_prefer_less_motion_get_no_animation(self):
        css = (BASE_DIR / "static" / "sidebar.css").read_text(encoding="utf-8")
        self.assertIn("@media (prefers-reduced-motion: reduce) { .rail-logo, .rail-logo img { animation: none !important;", css)


class FooterTests(TestCase):
    """Version and copyright are in a page footer, not at the bottom of the sidebar (which is all Object Browser)."""

    def page(self):
        user = get_user_model().objects.create_user("foot", password="pw-12345-abc", is_staff=True)
        self.client.force_login(user)
        return self.client.get("/").content.decode()

    def test_the_footer_is_at_the_end_of_the_main_area_with_the_version(self):
        html = self.page()
        main = html[html.index("<main"):html.index("</main>")]
        footer = main[main.index('<footer class="app-footer"'):]
        self.assertIn("Version: Development", footer)
        self.assertIn("PGDAS contributors", footer)
        self.assertIn("PostgreSQL Database Access System", footer)
        self.assertGreater(main.index("<footer"), main.index('class="content"'))                    # after the content ...
        self.assertTrue(main[:main.index("<footer")].rstrip().endswith("</div>"))                    # ... right after the content block closed
        self.assertEqual(html.count("Version: Development"), 1)

    def test_the_sidebar_has_nothing_below_the_tree(self):
        html = self.page()
        side = html[html.index("<aside"):html.index("</aside>")]
        self.assertNotIn("Version", side)
        self.assertNotIn("contributors", side)
        self.assertNotIn("sidebar-bottom", html)
        self.assertGreater(side.index('id="sidebarResizer"'), side.index('id="tree"'))

    def test_the_footer_stays_at_the_bottom_of_a_short_page(self):
        css = (BASE_DIR / "static" / "sidebar.css").read_text(encoding="utf-8")
        for needle in ("main { display: flex; flex-direction: column; }", "main > .content { flex: 1 0 auto;", ".app-footer {", ".app-footer span { display: inline; }"):
            self.assertIn(needle, css)
