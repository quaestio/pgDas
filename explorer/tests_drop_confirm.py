"""Dropping a database asks for its NAME (with the live message and the warning sound), as deleting a backup does.
Everything else that asks for a word (DROP TABLE, DROP ROLE, TRUNCATE, ...) keeps its short capital-letter word."""
import json
from pathlib import Path

from django.test import Client
from pgdas.testing import TestCase
from django.urls import reverse

from audit.models import AuditEntry

from . import changes, pg
from .tests import admin_connect
from .tests_admin import AdminFixture, DB, ROLE

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPT = "Type the database name to confirm"


class DropDatabaseNameTests(AdminFixture, TestCase):
    def make(self, name=DB):
        with admin_connect("postgres") as conn:
            conn.execute(f'CREATE DATABASE "{name}"')

    def exists(self, name=DB):
        with admin_connect("postgres") as conn:
            return bool(conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone())

    def test_the_preview_asks_for_the_name_and_says_how_to_prompt_for_it(self):
        self.make()
        data = self.preview(f"DROP DATABASE {DB}")
        self.assertEqual((data["confirm_word"], data["confirm_prompt"]), (DB, PROMPT))
        self.assertNotIn(DB, data["confirm_prompt"])                                # the prompt does not spell the name out
        self.assertEqual(data["button"], "Drop database")

    def test_the_short_word_is_still_asked_for_everything_else(self):
        with admin_connect(self.db) as conn:
            conn.execute(f"CREATE ROLE {ROLE}")
        role = self.preview(f"DROP ROLE {ROLE}")
        self.assertEqual((role["confirm_word"], role["confirm_prompt"]), ("DROP", None))
        table = changes.preview(self.server, self.db, "DROP TABLE pgdas_w.logged")
        self.assertEqual(table["confirm_word"], "DROP")
        self.assertNotIn("confirm_prompt", table)

    def test_the_wrong_text_is_refused_and_nothing_is_dropped(self):
        self.make()
        data = self.preview(f"DROP DATABASE {DB}")
        for wrong in (None, "", "DROP", "drop", DB.upper(), DB + " ", " " + DB, DB[:-1], DB + "x"):
            with self.subTest(typed=wrong), self.assertRaisesRegex(pg.PgError, "Type the database name exactly to confirm"):
                self.execute(f"DROP DATABASE {DB}", data["fingerprint"], wrong)
        self.assertTrue(self.exists())
        self.assertFalse(AuditEntry.objects.exists())

    def test_the_exact_name_drops_it_and_is_audited(self):
        self.make()
        sql = f"DROP DATABASE {DB}"
        data = self.preview(sql)
        result = self.execute(sql, data["fingerprint"], DB)
        self.assertFalse(self.exists())
        self.assertEqual(AuditEntry.objects.get(pk=result["entry_id"]).payload["object_type"], "DATABASE")

    def test_a_quoted_mixed_case_name_must_be_typed_exactly_as_it_is(self):
        mixed = "PgdasT_Mixed Case"
        self.addCleanup(lambda: admin_connect("postgres").execute(f'DROP DATABASE IF EXISTS "{mixed}" WITH (FORCE)'))
        self.make(mixed)
        sql = f'DROP DATABASE "{mixed}"'
        data = self.preview(sql)
        self.assertEqual(data["confirm_word"], mixed)
        with self.assertRaisesRegex(pg.PgError, "exactly"):
            self.execute(sql, data["fingerprint"], mixed.lower())
        self.execute(sql, data["fingerprint"], mixed)
        self.assertFalse(self.exists(mixed))

    def test_the_sql_runner_endpoint_sends_the_same_prompt(self):
        self.make()
        client = Client()
        client.force_login(self.staff)
        reply = client.post(reverse("write_preview", args=[self.server.pk]), json.dumps({"sql": f"DROP DATABASE {DB}", "db": self.db}),
                            content_type="application/json").json()
        self.assertTrue(reply["ok"])
        self.assertEqual((reply["confirm_word"], reply["confirm_prompt"]), (DB, PROMPT))

    def test_the_page_scripts_and_styles_are_loaded_everywhere_the_popup_can_open(self):
        client = Client()
        client.force_login(self.staff)
        html = client.get("/").content.decode()
        self.assertIn('src="/static/typedname.js"', html)
        self.assertIn('href="/static/confirm.css"', html)
        self.assertLess(html.index("typedname.js"), html.index("review.js"))              # the helper is there before the popup script runs

    def test_the_review_popup_switches_to_the_name_box_and_the_warning_sound(self):
        script = (BASE_DIR / "static" / "review.js").read_text(encoding="utf-8")
        for needle in ("byName: !!preview.confirm_prompt", "setConfirmMode(checked.byName, preview.confirm_prompt)",
                       "pgdasTypedName.describe(confirmInput.value, checked.word, goButton.textContent)",
                       'tone(checked.byName ? "warning" : "error")', 'confirmInput.removeAttribute("maxlength")', 'role", "status"'):
            self.assertIn(needle, script)
        self.assertIn('confirmInput.classList.toggle("confirm-name", byName)', script)
        self.assertIn('classList.toggle("zap", !byName)', script)                           # other words keep the capital-letter field
        self.assertIn('confirmInput.classList.remove("match", "wrong")', script)             # no green or red border carried over
        self.assertNotIn("innerHTML = preview", script)                                      # still nothing from the database as HTML


class TypedNameMessagesTests(TestCase):
    def test_the_messages_live_in_one_shared_file_and_never_contain_the_word(self):
        script = (BASE_DIR / "static" / "typedname.js").read_text(encoding="utf-8")
        for message in ("Type the name to enable", "Matches. Press", "Keep typing", "Remove the extra characters after the name.",
                        "Capital letters matter: the name must match exactly.", "That does not match. Check the spelling."):
            self.assertIn(message, script)
        for line in script.splitlines():
            if "text:" in line:
                self.assertNotIn("+ word +", line)
        for page in ("confirm.js", "review.js"):
            self.assertIn("pgdasTypedName.describe", (BASE_DIR / "static" / page).read_text(encoding="utf-8"), page)
