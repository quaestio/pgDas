"""Deleting a backup: a popup that asks for the database name (instead of the browser's OK box), and the server insisting on it."""
from pathlib import Path

from django.test import TestCase
from django.urls import reverse

from audit.models import AuditEntry

from . import dump
from .models import Backup
from .tests import BackupFixture

BASE_DIR = Path(__file__).resolve().parent.parent


class DeleteNeedsTheDatabaseNameTests(BackupFixture, TestCase):
    def delete(self, backup, **data):
        return self.client.post(reverse("backup_delete", args=[backup.pk]), data, follow=True)

    def test_without_the_name_nothing_is_deleted(self):
        backup = dump.make_backup(self.server, self.db, self.staff)
        for data in ({}, {"confirm": ""}, {"confirm": "yes"}, {"confirm": self.db.upper()}, {"confirm": self.db + " "}, {"confirm": "DELETE"}):
            with self.subTest(data=data):
                response = self.delete(backup, **data)
                self.assertContains(response, f"Type the database name ({self.db}) exactly")
                self.assertContains(response, "Nothing was deleted")
                self.assertTrue(Backup.objects.filter(pk=backup.pk).exists())
                self.assertTrue(backup.path.is_file())
        self.assertFalse(AuditEntry.objects.filter(action="backup.delete").exists())

    def test_with_the_exact_name_the_file_and_the_row_go_and_it_is_audited(self):
        backup = dump.make_backup(self.server, self.db, self.staff)
        path = backup.path
        response = self.delete(backup, confirm=self.db)
        self.assertContains(response, "Backup deleted.")
        self.assertFalse(path.exists())
        self.assertFalse(Backup.objects.filter(pk=backup.pk).exists())
        self.assertTrue(AuditEntry.objects.filter(action="backup.delete", ok=True).exists())

    def test_a_failed_backup_with_no_file_is_removed_the_same_way(self):
        backup = dump.make_backup(self.server, "pgdas_no_such_db", self.staff)
        self.assertEqual(backup.status, "failed")
        self.delete(backup, confirm="wrong")
        self.assertTrue(Backup.objects.filter(pk=backup.pk).exists())
        self.delete(backup, confirm="pgdas_no_such_db")
        self.assertFalse(Backup.objects.filter(pk=backup.pk).exists())

    def test_only_a_post_deletes(self):
        backup = dump.make_backup(self.server, self.db, self.staff)
        self.assertEqual(self.client.get(reverse("backup_delete", args=[backup.pk]), {"confirm": self.db}).status_code, 405)
        self.assertTrue(Backup.objects.filter(pk=backup.pk).exists())


class DeletePopupTests(BackupFixture, TestCase):
    def test_the_button_opens_the_popup_and_the_browser_box_is_gone(self):
        backup = dump.make_backup(self.server, self.db, self.staff)
        html = self.client.get(reverse("backup_list")).content.decode()
        form = html[html.index(f'action="/backups/{backup.pk}/delete/"'):]
        form = form[:form.index("</form>")]
        self.assertIn(f'data-typed-confirm="{self.db}"', form)
        self.assertIn('data-confirm-title="DELETE BACKUP"', form)
        self.assertIn('data-confirm-prompt="Type the database name to confirm"', form)      # the prompt does not spell the name out
        self.assertIn("permanently deletes the backup file", form)
        self.assertIn('name="confirm" value=""', form)
        self.assertIn("cannot be recovered", form)
        self.assertNotIn("onsubmit", html[html.index('<section class="panel pad scroll">'):])    # no browser confirm() any more
        self.assertNotIn("confirm('Delete", html)
        self.assertIn('src="/static/confirm.js"', html)
        self.assertIn('href="/static/confirm.css"', html)

    def test_a_failed_backup_says_there_is_no_file(self):
        backup = dump.make_backup(self.server, "pgdas_no_such_db", self.staff)
        html = self.client.get(reverse("backup_list")).content.decode()
        form = html[html.index(f'action="/backups/{backup.pk}/delete/"'):]
        self.assertIn("there is no file", form[:form.index("</form>")])

    def test_the_script_asks_for_the_exact_name_plays_the_warning_and_traps_focus(self):
        script = (BASE_DIR / "static" / "confirm.js").read_text(encoding="utf-8")
        for needle in ("typed === word", "parts.go.disabled = !(pending && typed === word)", 'play("warning")', '"Escape"', '"Tab"',
                       'aria-modal', "HTMLFormElement.prototype.submit.call(form)", "textContent"):
            self.assertIn(needle, script)
        self.assertNotIn("innerHTML", script)                                         # names and messages are put in as text only
        self.assertIn("data.confirmPrompt", script)
        self.assertNotIn('make("strong")', script)                                    # the prompt line has no <strong>name</strong> any more
        self.assertNotIn("parts.word", script)
        css = (BASE_DIR / "static" / "confirm.css").read_text(encoding="utf-8")
        for needle in (".confirm-dialog", "input.confirm-name", ".match", ".wrong", ".confirm-status.ok", ".confirm-status.partial",
                       "prefers-reduced-motion"):
            self.assertIn(needle, css)

    def test_a_live_line_tells_where_you_stand_without_spelling_the_name_out(self):
        script = (BASE_DIR / "static" / "confirm.js").read_text(encoding="utf-8")
        helper = (BASE_DIR / "static" / "typedname.js").read_text(encoding="utf-8")        # the wording is shared with the drop-database popup
        for message in ("Type the name to enable", "Keep typing", "Remove the extra characters after the name.",
                        "Capital letters matter: the name must match exactly.", "That does not match. Check the spelling.", "Matches. Press"):
            self.assertIn(message, helper)
        self.assertIn("pgdasTypedName.describe", script)
        self.assertIn('setAttribute("role", "status")', script)                       # read out by screen readers as it changes
        self.assertIn('setAttribute("aria-live", "polite")', script)
        for line in helper.splitlines():                                              # no message includes the word being typed
            if "text:" in line:
                self.assertNotIn("+ word +", line.replace(" word.length", ""))

    def test_the_warning_sound_is_two_descending_blips_and_respects_the_mute_button(self):
        sound = (BASE_DIR / "static" / "sound.js").read_text(encoding="utf-8")
        self.assertIn("warning: [540, 300]", sound)                                   # falling pitch
        self.assertIn("var BEATS = {warning: [0, 0.18]};", sound)                    # two blips
        self.assertIn("if (!on) return;", sound)                                      # muted: silent
