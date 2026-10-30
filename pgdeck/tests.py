"""PgDeck's own storage is one SQLite file: no database server is needed to install or run it."""
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

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
        env = dict(os.environ, PGDECK_DEBUG="1")
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
        self.assertIn("pgdeck-data:/app/data", compose)         # the SQLite file is kept in a volume
        self.assertIn("PGDECK_SQLITE_PATH", compose)
        dockerfile = (BASE_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("VOLUME", dockerfile)
        # The image must never contain the local .env, the SQLite data or the virtualenv.
        ignore = (BASE_DIR / ".dockerignore").read_text(encoding="utf-8").split()
        for entry in (".env", "data", ".venv"):
            self.assertIn(entry, ignore)
        example = (BASE_DIR / ".env.example").read_text(encoding="utf-8")
        self.assertNotIn("PGDECK_DB_PASSWORD", example.replace("PGDECK_TEST_PG_PASSWORD", ""))
        self.assertIn("PGDECK_SQLITE_PATH", example)
        readme = (BASE_DIR / "README.md").read_text(encoding="utf-8")
        self.assertIn("no database server of its own", readme)
        self.assertNotIn("Create the settings database first", readme)

    def test_a_fresh_install_needs_no_database_server_settings(self):
        """Run migrate in a clean process with no PGDECK_DB_* settings and a temporary SQLite path."""
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "nested" / "pgdeck.sqlite3"
            env = {key: value for key, value in os.environ.items() if not key.startswith(("PGDECK_DB_", "PGDECK_TEST_"))}
            env.update({"PGDECK_DEBUG": "1", "PGDECK_SQLITE_PATH": str(target)})
            result = subprocess.run([sys.executable, str(BASE_DIR / "manage.py"), "migrate", "--noinput"],
                                    cwd=BASE_DIR, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr[-800:])
            self.assertTrue(target.exists())                                    # the missing folder was created too
            with closing(sqlite3.connect(target)) as db:     # (sqlite3's own `with` does not close the file)
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                mode = db.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertTrue({"auth_user", "servers_serverconnection", "audit_auditentry", "django_session"} <= tables)
            self.assertEqual(mode, "wal")                                       # persisted in the file by init_command
