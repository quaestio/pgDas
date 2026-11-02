"""Backups: pg_dump to a file, download, restore into a new database, schedules and retention."""
import tempfile
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEntry
from explorer import pg
from explorer.tests import admin_connect
from explorer.tests_writes import WriteFixture

from . import dump
from .models import Backup, BackupSchedule

NEW_DB = "pgdeck_t_restored"


class ToolTests(SimpleTestCase):
    def test_a_missing_tool_folder_is_explained(self):
        with override_settings(PG_BIN="C:/does/not/exist"), self.assertRaisesRegex(pg.PgError, "PGDECK_PG_BIN"):
            dump.tool("pg_dump")


class BackupFixture(WriteFixture):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        override = override_settings(BACKUP_DIR=Path(folder.name))
        override.enable()
        self.addCleanup(override.disable)
        try:
            dump.tool("pg_dump")
            dump.tool("pg_restore")
        except pg.PgError as error:
            self.skipTest(str(error))
        self.drop_new()
        self.addCleanup(self.drop_new)
        self.client = Client()
        self.client.force_login(self.staff)

    def drop_new(self):
        with admin_connect("postgres") as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {NEW_DB} WITH (FORCE)")


class BackupTests(BackupFixture, TestCase):
    def test_backup_makes_a_file_and_an_audit_entry(self):
        backup = dump.make_backup(self.server, self.db, self.staff)
        self.assertEqual(backup.status, "ok")
        self.assertTrue(backup.available)
        self.assertGreater(backup.size, 0)
        self.assertTrue(AuditEntry.objects.filter(action="backup.create", ok=True).exists())

    def test_the_password_is_not_kept_anywhere(self):
        backup = dump.make_backup(self.server, self.db, self.staff)
        self.assertNotIn(self.server.get_password(), backup.error + " ".join(AuditEntry.objects.values_list("detail", flat=True)))

    def test_a_failed_backup_is_recorded_with_the_reason_and_leaves_no_file(self):
        backup = dump.make_backup(self.server, "pgdeck_no_such_db", self.staff)
        self.assertEqual(backup.status, "failed")
        self.assertIn("pgdeck_no_such_db", backup.error)
        self.assertFalse(backup.available)
        self.assertEqual(list(Path(dump.settings.BACKUP_DIR).glob("*")), [])
        self.assertTrue(AuditEntry.objects.filter(action="backup.create", ok=False).exists())

    def test_download_gives_the_file(self):
        backup = dump.make_backup(self.server, self.db, self.staff)
        response = self.client.get(reverse("backup_download", args=[backup.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content)[:5], b"PGDMP")
        self.assertIn("attachment", response["Content-Disposition"])

    def test_restore_goes_into_a_new_database(self):
        backup = dump.make_backup(self.server, self.db, self.staff)
        dump.restore_backup(backup, NEW_DB, self.staff)
        with admin_connect(NEW_DB) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM pgdeck_w.logged").fetchone()[0], 2)
        self.assertTrue(AuditEntry.objects.filter(action="backup.restore", ok=True).exists())

    def test_restore_refuses_an_existing_or_odd_name_and_keeps_what_exists(self):
        backup = dump.make_backup(self.server, self.db, self.staff)
        with self.assertRaisesRegex(pg.PgError, "already exists"):
            dump.restore_backup(backup, "postgres", self.staff)
        with self.assertRaisesRegex(pg.PgError, "plain name"):
            dump.restore_backup(backup, 'x"; DROP DATABASE postgres; --', self.staff)

    def test_the_restore_form_needs_the_typed_word(self):
        backup = dump.make_backup(self.server, self.db, self.staff)
        self.client.post(reverse("backup_restore", args=[backup.pk]), {"new_name": NEW_DB, "confirm": "restore"})
        with admin_connect("postgres") as conn:
            self.assertFalse(conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (NEW_DB,)).fetchone())
        self.client.post(reverse("backup_restore", args=[backup.pk]), {"new_name": NEW_DB, "confirm": dump.RESTORE_WORD})
        with admin_connect("postgres") as conn:
            self.assertTrue(conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (NEW_DB,)).fetchone())

    def test_delete_removes_the_file(self):
        backup = dump.make_backup(self.server, self.db, self.staff)
        path = backup.path
        self.client.post(reverse("backup_delete", args=[backup.pk]))
        self.assertFalse(path.exists())
        self.assertFalse(Backup.objects.filter(pk=backup.pk).exists())

    def test_the_database_dropdown_lists_the_databases_of_the_chosen_server(self):
        data = self.client.get(reverse("backup_databases"), {"server": self.server.pk}).json()
        self.assertIn(self.db, data["databases"])
        self.assertNotIn("template0", data["databases"])
        self.assertEqual(self.client.get(reverse("backup_databases"), {"server": 0}).status_code, 404)

    def test_only_staff_may_use_backups(self):
        other = get_user_model().objects.create_user("plain", password="pw-12345-abc")
        client = Client()
        client.force_login(other)
        self.assertEqual(client.get(reverse("backup_list")).status_code, 302)
        self.assertEqual(client.post(reverse("backup_create"), {"server": self.server.pk, "database": self.db}).status_code, 302)
        self.assertFalse(Backup.objects.exists())
        self.assertEqual(self.client.get(reverse("backup_list")).status_code, 200)


class ScheduleTests(BackupFixture, TestCase):
    def test_only_due_schedules_run_and_old_scheduled_backups_are_pruned(self):
        schedule = BackupSchedule.objects.create(server=self.server, database=self.db, every_hours=1, keep=2)
        self.assertEqual(len(dump.run_due()), 1)                              # never run: due
        self.assertEqual(dump.run_due(), [])                                  # just run: not due
        manual = dump.make_backup(self.server, self.db, self.staff)           # manual ones are never pruned
        for _ in range(3):
            BackupSchedule.objects.filter(pk=schedule.pk).update(last_run=timezone.now() - timedelta(hours=2))
            self.assertEqual(len(dump.run_due()), 1)
        scheduled = Backup.objects.filter(kind="scheduled", status="ok")
        self.assertEqual(scheduled.count(), 2)
        self.assertTrue(all(b.available for b in scheduled))
        self.assertTrue(Backup.objects.get(pk=manual.pk).available)
        self.assertEqual(len(list(Path(dump.settings.BACKUP_DIR).glob("*.dump"))), 3)

    def test_the_schedule_form_checks_its_numbers(self):
        client = Client()
        client.force_login(self.staff)
        client.post(reverse("schedule_save"), {"server": self.server.pk, "database": self.db, "every_hours": "0", "keep": "3"})
        self.assertFalse(BackupSchedule.objects.exists())
        client.post(reverse("schedule_save"), {"server": self.server.pk, "database": self.db, "every_hours": "6", "keep": "3"})
        self.assertEqual(BackupSchedule.objects.get().every_hours, 6)
