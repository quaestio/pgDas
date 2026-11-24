"""The setup check: what is reported, who sees what, and the test email."""
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from access_control import readiness
from access_control.models import AlertRecipient, Installation, Membership, Organization, OrganizationServer
from servers.models import ServerConnection
from watch.models import ServerHealth, WatchRun

MAIL = dict(EMAIL_HOST="smtp.test", EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")


def by_id(checks):
    return {c["id"]: c for c in checks}


class Fixture(TestCase):
    def setUp(self):
        cache.clear()
        mail.outbox = []
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Org")
        self.admin = get_user_model().objects.create_user("a", email="a@o.test", password="pw-12345-abc", is_staff=True, is_superuser=True, email_verified=True)
        Membership.objects.create(user=self.admin, organization=self.org, role="super_user")
        self.client = Client()
        self.client.force_login(self.admin)


class OrganizationChecksTests(Fixture):
    def test_a_new_organization_is_told_what_to_do_first(self):
        checks = by_id(readiness.organization_checks(self.org, self.admin))
        self.assertEqual(checks["servers"]["status"], "warn")
        self.assertIn("Add server", checks["servers"]["fix"])
        self.assertEqual(checks["alerts"]["status"], "warn")
        self.assertEqual(checks["account_email"]["status"], "ok")
        self.assertTrue(all(c["scope"] == "organization" for c in checks.values()))

    def test_servers_backups_monitoring_and_alerts_turn_green_as_they_are_set_up(self):
        server = ServerConnection.objects.create(name="Prod", host="h", username="u")
        OrganizationServer.objects.create(server=server, organization=self.org)
        checks = by_id(readiness.organization_checks(self.org, self.admin))
        self.assertEqual((checks["servers"]["status"], checks["monitoring"]["status"], checks["backups"]["status"]), ("ok", "warn", "warn"))
        ServerHealth.objects.create(server=server, sample_count=5)
        from backups.models import BackupSchedule
        BackupSchedule.objects.create(server=server, database="postgres", every_hours=24, keep=3) if hasattr(BackupSchedule, "every_hours") else None
        AlertRecipient.objects.create(organization=self.org, email="ops@o.test", verified=True)
        with override_settings(**MAIL, MODE="shared"):
            checks = by_id(readiness.organization_checks(self.org, self.admin))
        self.assertEqual((checks["monitoring"]["status"], checks["alerts"]["status"]), ("ok", "ok"))

    def test_locked_and_unreachable_servers_are_named(self):
        locked = ServerConnection.objects.create(name="Locked", host="h", username="u", save_password=False)
        down = ServerConnection.objects.create(name="Down", host="h2", username="u")
        for s in (locked, down):
            OrganizationServer.objects.create(server=s, organization=self.org)
        ServerHealth.objects.create(server=down, failures=3, last_error="connection refused", sample_count=1)
        checks = by_id(readiness.organization_checks(self.org, self.admin))
        self.assertIn("Locked", checks["locked"]["detail"])
        self.assertIn("Down (connection refused", checks["unreachable"]["detail"])

    def test_another_organizations_servers_are_not_counted(self):
        other = Organization.objects.create(name="Other")
        server = ServerConnection.objects.create(name="Theirs", host="h", username="u")
        OrganizationServer.objects.create(server=server, organization=other)
        checks = by_id(readiness.organization_checks(self.org, self.admin))
        self.assertEqual(checks["servers"]["status"], "warn")

    def test_an_unverified_account_is_flagged(self):
        self.admin.email_verified = False
        self.assertEqual(by_id(readiness.organization_checks(self.org, self.admin))["account_email"]["status"], "warn")


class InstallationChecksTests(Fixture):
    @override_settings(DEBUG=True)
    def test_debug_and_the_development_key_are_failures(self):
        checks = by_id(readiness.installation_checks())
        self.assertEqual(checks["debug"]["status"], "fail")

    @override_settings(DEBUG=False, SECRET_KEY="insecure-development-key-do-not-use-in-production")
    def test_the_built_in_secret_key_is_a_failure_with_the_command_to_fix_it(self):
        check = by_id(readiness.installation_checks())["secret"]
        self.assertEqual(check["status"], "fail")
        self.assertIn("get_random_secret_key", check["fix"])

    @override_settings(DEBUG=False, CSRF_TRUSTED_ORIGINS=[], SESSION_COOKIE_SECURE=False)
    def test_production_without_https_origins_or_secure_cookies_warns(self):
        checks = by_id(readiness.installation_checks())
        self.assertEqual((checks["origins"]["status"], checks["cookies"]["status"]), ("warn", "warn"))

    @override_settings(DEBUG=False, CSRF_TRUSTED_ORIGINS=["https://pg.example.test"], SESSION_COOKIE_SECURE=True)
    def test_https_origins_and_cookies_are_fine_when_set(self):
        checks = by_id(readiness.installation_checks())
        self.assertEqual((checks["origins"]["status"], checks["cookies"]["status"]), ("ok", "ok"))

    @override_settings(BEHIND_HTTPS_PROXY=False)
    def test_a_proxy_that_is_not_trusted_is_noticed_from_the_request(self):
        from django.test import RequestFactory
        request = RequestFactory().get("/", HTTP_X_FORWARDED_PROTO="https")
        self.assertEqual(by_id(readiness.installation_checks(request))["proxy"]["status"], "warn")
        with override_settings(BEHIND_HTTPS_PROXY=True):
            self.assertEqual(by_id(readiness.installation_checks(request))["proxy"]["status"], "ok")

    def test_email_is_a_failure_for_open_registration_without_a_mail_server_and_ok_with_one(self):
        with override_settings(MODE="shared", ALLOW_ORGANIZATION_REGISTRATION=True, EMAIL_HOST=""):
            self.assertEqual(by_id(readiness.installation_checks())["email"]["status"], "fail")
        with override_settings(EMAIL_HOST="", MODE="dedicated"):
            self.assertEqual(by_id(readiness.installation_checks())["email"]["status"], "warn")
        with override_settings(**MAIL):
            self.assertEqual(by_id(readiness.installation_checks())["email"]["status"], "ok")

    def test_the_cron_jobs_are_checked_by_when_they_last_ran(self):
        self.assertEqual(by_id(readiness.installation_checks())["run_watch"]["status"], "warn")
        self.assertIn("run_watch", by_id(readiness.installation_checks())["run_watch"]["fix"])
        WatchRun.objects.create(started_at=timezone.now() - timedelta(minutes=1), finished_at=timezone.now(), sampled=2)
        self.assertEqual(by_id(readiness.installation_checks())["run_watch"]["status"], "ok")
        WatchRun.objects.update(started_at=timezone.now() - timedelta(hours=3))
        check = by_id(readiness.installation_checks())["run_watch"]
        self.assertEqual(check["status"], "warn")
        self.assertIn("hours ago", check["detail"])

    def test_folders_that_cannot_be_written_are_failures_with_the_command_to_fix(self):
        from unittest import mock
        with mock.patch("access_control.readiness._writable", return_value=False):
            checks = by_id(readiness.installation_checks())
        self.assertEqual((checks["data_dir"]["status"], checks["backup_dir"]["status"]), ("fail", "fail"))
        self.assertIn("chown", checks["data_dir"]["fix"])

    def test_django_system_checks_are_included(self):
        with override_settings(MODE="shared", ALLOW_ORGANIZATION_REGISTRATION=True, EMAIL_HOST=""):
            ids = [c["id"] for c in readiness.installation_checks()]
        self.assertIn("check-access_control.W004", ids)

    def test_a_check_that_cannot_run_never_raises(self):
        from unittest import mock
        with mock.patch("backups.dump.tool", side_effect=RuntimeError("odd")):
            self.assertEqual(by_id(readiness.installation_checks())["pg_dump"]["status"], "warn")

    def test_the_summary_counts_each_status(self):
        counts = readiness.summary([{"status": "ok"}, {"status": "ok"}, {"status": "warn"}, {"status": "fail"}, {"status": "info"}])
        self.assertEqual(counts, {"ok": 2, "info": 1, "warn": 1, "fail": 1})


class PageTests(Fixture):
    @override_settings(MODE="dedicated", **MAIL)
    def test_a_dedicated_installation_shows_the_machine_checks_and_the_organizations(self):
        page = self.client.get(reverse("readiness"))
        for needle in ("Setup check", "Production settings", "Servers", "Alert notifications", "Send a test email to me"):
            self.assertContains(page, needle)

    @override_settings(MODE="shared", **MAIL)
    def test_a_shared_installation_hides_the_machine_checks_from_organizations(self):
        page = self.client.get(reverse("readiness"))
        self.assertContains(page, "Servers")
        for secret in ("Production settings", "Secret key", "Allowed host names", "Backup folder"):
            self.assertNotContains(page, secret)
        self.assertContains(page, "belong to the person who runs PGDAS")

    def test_the_worst_problems_come_first(self):
        statuses = [c["status"] for c in self.client.get(reverse("readiness")).context["checks"]]
        order = {"fail": 0, "warn": 1, "info": 2, "ok": 3}
        self.assertEqual(statuses, sorted(statuses, key=order.get))

    def test_only_super_users_open_it(self):
        person = get_user_model().objects.create_user("p", email="p@o.test", password="pw-12345-abc")
        Membership.objects.create(user=person, organization=self.org, role="editor")
        other = Client()
        other.force_login(person)
        self.assertEqual(other.get(reverse("readiness")).status_code, 403)
        self.assertEqual(other.post(reverse("readiness_mail")).status_code, 403)

    def test_the_employees_page_links_to_it(self):
        self.assertContains(self.client.get(reverse("employee_list")), reverse("readiness"))


class TestEmailTests(Fixture):
    @override_settings(**MAIL)
    def test_it_sends_one_test_email_to_the_signed_in_person_only(self):
        response = self.client.post(reverse("readiness_mail"))
        self.assertTrue(response.json()["ok"], response.content)
        self.assertEqual([m.to for m in mail.outbox], [["a@o.test"]])

    @override_settings(EMAIL_HOST="")
    def test_without_a_mail_server_it_says_so(self):
        self.assertEqual(self.client.post(reverse("readiness_mail")).status_code, 400)

    @override_settings(**MAIL)
    def test_it_is_limited_to_five_an_hour(self):
        statuses = [self.client.post(reverse("readiness_mail")).status_code for _ in range(7)]
        self.assertEqual(statuses, [200] * 5 + [429] * 2)

    @override_settings(**MAIL)
    def test_a_refusal_by_the_mail_server_is_reported_not_hidden(self):
        from unittest import mock
        with mock.patch("accounts.mailer.send_mail", side_effect=OSError("smtp down")), self.assertLogs("pgdas.mail", "ERROR"):
            response = self.client.post(reverse("readiness_mail"))
        self.assertEqual(response.status_code, 502)
        self.assertIn("refused", response.json()["error"])


class CommandTests(Fixture):
    def test_the_host_command_prints_problems_with_their_fixes(self):
        out = StringIO()
        with override_settings(DEBUG=True):
            call_command("readiness", "--problems", stdout=out)
        text = out.getvalue()
        self.assertIn("[FAIL] Production settings", text)
        self.assertIn("fix:", text)
        self.assertRegex(text, r"\d+ failing, \d+ warning, \d+ fine")