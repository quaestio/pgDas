"""Backup options: only some schemas or tables, the data of some tables left out, compression. Real pg_dump and pg_restore."""
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEntry
from explorer import pg
from explorer.tests import admin_connect

from . import dump
from .models import Backup, BackupSchedule
from .tests import NEW_DB, BackupFixture


class ParseOptionsTests(SimpleTestCase):
    def test_everything_by_default(self):
        self.assertEqual(dump.parse_options(), {})
        self.assertEqual(dump.parse_options([], "", "  ", ""), {})
        self.assertEqual(dump.describe({}), "")

    def test_names_come_one_per_line_or_comma_separated_without_repeats(self):
        options = dump.parse_options(["sales", "hr"], "public.orders\r\nsales.*, public.orders", "public.log_*", "3")
        self.assertEqual(options, {"schemas": ["sales", "hr"], "tables": ["public.orders", "sales.*"], "skip_data": ["public.log_*"], "compression": 3})
        self.assertEqual(dump.describe(options), "only schemas: sales, hr · only tables: public.orders, sales.* · without the data of: public.log_* · compression 3")
        self.assertEqual(dump.parse_options(compression="0"), {"compression": 0})

    def test_nothing_that_could_pass_for_an_option_or_a_command_is_accepted(self):
        for bad in ("public.orders; DROP DATABASE x", "a b", "x'y", "$(rm -rf)", "t`x`", "a/b", "a\\b", "x" * 201):
            with self.subTest(bad=bad), self.assertRaisesRegex(pg.PgError, "not a valid"):
                dump.parse_options(tables=bad)
            with self.subTest(schema=bad), self.assertRaisesRegex(pg.PgError, "not a valid schema name"):
                dump.parse_options(schemas=[bad])
        for bad in ("10", "-1", "x", "1.5"):
            with self.subTest(compression=bad), self.assertRaisesRegex(pg.PgError, "0 \\(none\\) to 9"):
                dump.parse_options(compression=bad)
        with self.assertRaisesRegex(pg.PgError, "at most 50"):
            dump.parse_options(tables="\n".join(f"t{i}" for i in range(51)))

    def test_every_value_is_one_long_option_with_an_equals_sign(self):
        args = dump._option_args({"schemas": ["a"], "tables": ["--help"], "skip_data": ["b.*"], "compression": 9})
        self.assertEqual(args, ["--schema=a", "--table=--help", "--exclude-table-data=b.*", "--compress=9"])   # "--help" is only a value here


class OptionBackupTests(BackupFixture, TestCase):
    def restored(self, backup):
        warnings = dump.restore_backup(backup, NEW_DB, self.staff)
        return warnings, lambda sql: self.query_new(sql)

    def query_new(self, sql):
        with admin_connect(NEW_DB) as conn:
            return conn.execute(sql).fetchall()

    def test_only_the_chosen_tables_are_saved_and_their_schema_is_recreated(self):
        backup = dump.make_backup(self.server, self.db, self.staff, options=dump.parse_options(tables="pgdas_w.logged"))
        self.assertEqual((backup.status, backup.partial), ("ok", True), backup.error)
        warnings, q = self.restored(backup)
        self.assertEqual(q("SELECT count(*) FROM pgdas_w.logged")[0][0], 2)
        self.assertEqual(q("SELECT to_regclass('pgdas_w.parent')")[0][0], None)                  # not saved
        self.assertEqual(q("SELECT to_regclass('pgdas_w.child')")[0][0], None)
        self.assertEqual(q("SELECT 1 FROM pg_trigger WHERE tgname = 'logged_delete'"), [])        # its function was not saved: reported, not fatal
        self.assertTrue(warnings)

    def test_only_the_chosen_schema_is_saved_with_everything_in_it(self):
        with admin_connect(self.db) as conn:
            conn.execute("DROP SCHEMA IF EXISTS pgdas_other CASCADE")
            conn.execute("CREATE SCHEMA pgdas_other")
            conn.execute("CREATE TABLE pgdas_other.t (a int)")
        self.addCleanup(lambda: admin_connect(self.db).execute("DROP SCHEMA IF EXISTS pgdas_other CASCADE"))
        backup = dump.make_backup(self.server, self.db, self.staff, options=dump.parse_options(schemas=["pgdas_w"]))
        self.assertEqual(backup.status, "ok", backup.error)
        _, q = self.restored(backup)
        self.assertEqual(q("SELECT count(*) FROM pgdas_w.parent")[0][0], 10)
        self.assertEqual(q("SELECT count(*) FROM pgdas_w.child")[0][0], 20)
        self.assertEqual(q("SELECT to_regclass('pgdas_other.t')")[0][0], None)

    def test_the_data_of_some_tables_can_be_left_out_and_their_structure_stays(self):
        backup = dump.make_backup(self.server, self.db, self.staff, options=dump.parse_options(skip_data="pgdas_w.logged, pgdas_w.log"))
        self.assertEqual(backup.status, "ok", backup.error)
        _, q = self.restored(backup)
        self.assertEqual(q("SELECT count(*) FROM pgdas_w.logged")[0][0], 0)
        self.assertEqual(q("SELECT count(*) FROM pgdas_w.parent")[0][0], 10)
        self.assertEqual(q("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'pgdas_w' AND table_name = 'logged'")[0][0], 2)

    def test_skipping_the_data_of_a_referenced_table_still_restores_with_a_warning(self):
        backup = dump.make_backup(self.server, self.db, self.staff, options=dump.parse_options(skip_data="pgdas_w.parent"))
        warnings, q = self.restored(backup)                          # the foreign key cannot be checked: reported, not fatal
        self.assertEqual((q("SELECT count(*) FROM pgdas_w.parent")[0][0], q("SELECT count(*) FROM pgdas_w.child")[0][0]), (0, 20))
        self.assertIn("errors ignored on restore", warnings)

    def test_a_full_backup_is_still_strict_and_has_no_options(self):
        backup = dump.make_backup(self.server, self.db, self.staff)
        self.assertEqual((backup.options, backup.partial, backup.options_text), ({}, False, ""))
        self.assertEqual(dump.restore_backup(backup, NEW_DB, self.staff), "")

    def test_compression_levels_make_valid_files(self):
        for level in (0, 9):
            with self.subTest(level=level):
                backup = dump.make_backup(self.server, self.db, self.staff, options=dump.parse_options(compression=str(level)))
                self.assertEqual(backup.status, "ok", backup.error)
                self.assertEqual(backup.path.read_bytes()[:5], b"PGDMP")
                self.assertIn(f"compression {level}", backup.options_text)

    def test_an_option_that_matches_nothing_fails_with_postgresqls_reason(self):
        backup = dump.make_backup(self.server, self.db, self.staff, options=dump.parse_options(schemas=["pgdas_no_such_schema"]))
        self.assertEqual(backup.status, "failed")
        self.assertIn("no matching schemas", backup.error)
        self.assertFalse(backup.available)

    def test_the_audit_entry_says_what_was_chosen(self):
        dump.make_backup(self.server, self.db, self.staff, options=dump.parse_options(skip_data="pgdas_w.log"))
        self.assertIn("without the data of: pgdas_w.log", AuditEntry.objects.get(action="backup.create").detail)


class OptionFormTests(BackupFixture, TestCase):
    def test_the_back_up_form_passes_the_options_on_and_the_list_shows_them(self):
        response = self.client.post(reverse("backup_create"), {
            "server": self.server.pk, "database": self.db, "schemas": ["pgdas_w"], "tables": "", "skip_data": "pgdas_w.log", "compression": "5"},
            follow=True)
        backup = Backup.objects.get()
        self.assertEqual(backup.options, {"schemas": ["pgdas_w"], "skip_data": ["pgdas_w.log"], "compression": 5})
        self.assertContains(response, "only schemas: pgdas_w")
        self.assertContains(response, "without the data of: pgdas_w.log")
        self.assertContains(response, '<span class="badge">partial</span>')

    def test_a_bad_name_is_refused_with_a_message_and_nothing_is_made(self):
        for field, value in (("tables", "x; DROP TABLE y"), ("compression", "11")):
            with self.subTest(field=field):
                response = self.client.post(reverse("backup_create"), {"server": self.server.pk, "database": self.db, field: value}, follow=True)
                self.assertFalse(Backup.objects.exists())
                self.assertContains(response, "not a valid" if field == "tables" else "0 (none) to 9")

    def test_a_schedule_keeps_its_options_and_each_run_uses_them(self):
        self.client.post(reverse("schedule_save"), {"server": self.server.pk, "database": self.db, "every_hours": "1", "keep": "3",
                                                    "skip_data": "pgdas_w.log", "compression": "2"})
        schedule = BackupSchedule.objects.get()
        self.assertEqual(schedule.options, {"skip_data": ["pgdas_w.log"], "compression": 2})
        self.assertIn("without the data of", self.client.get(reverse("backup_list")).content.decode())
        made = dump.run_due(timezone.now() + timedelta(seconds=1))
        self.assertEqual([(b.status, b.kind, b.options) for b in made], [("ok", "scheduled", schedule.options)])
        self.client.post(reverse("schedule_save"), {"server": self.server.pk, "database": self.db, "every_hours": "6", "keep": "3"})
        self.assertEqual(BackupSchedule.objects.get().options, {})                   # saving again without options clears them

    def test_the_schema_choices_list_user_schemas_only_and_need_staff(self):
        data = self.client.get(reverse("backup_schemas"), {"server": self.server.pk, "database": self.db}).json()
        self.assertIn("pgdas_w", data["schemas"])
        self.assertTrue(all(not s.startswith("pg_") and s != "information_schema" for s in data["schemas"]))
        self.assertEqual(self.client.get(reverse("backup_schemas"), {"server": self.server.pk, "database": ""}).json(), {"schemas": []})
        self.assertIn("error", self.client.get(reverse("backup_schemas"), {"server": self.server.pk, "database": "pgdas_no_such_db"}).json())
        plain = Client()
        plain.force_login(get_user_model().objects.create_user("plain", password="pw-12345-abc"))
        self.assertEqual(plain.get(reverse("backup_schemas"), {"server": self.server.pk, "database": self.db}).status_code, 302)
