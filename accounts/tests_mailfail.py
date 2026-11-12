"""Email failures are no longer silent: they are logged, audited, and the page says so."""
import smtplib
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from access_control.models import Installation, Membership, Organization
from audit.models import AuditEntry

EMAIL = dict(EMAIL_HOST="smtp.test", EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
BROKEN = mock.patch("accounts.mailer.send_mail", side_effect=smtplib.SMTPAuthenticationError(535, b"bad login"))


@override_settings(MODE="shared", ALLOW_ORGANIZATION_REGISTRATION=True, **EMAIL)
class MailFailureTests(TestCase):
    def setUp(self):
        cache.clear()
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Org")
        self.admin = get_user_model().objects.create_user("a", email="a@o.test", password="pw-12345-abc", is_staff=True, is_superuser=True, email_verified=True)
        Membership.objects.create(user=self.admin, organization=self.org, role="super_user")

    def test_registration_says_it_could_not_send_and_logs_and_audits_it(self):
        with BROKEN, self.assertLogs("pgdas.mail", "ERROR") as logs:
            response = Client().post(reverse("register"), {"organization": "N", "name": "B", "designation": "", "email": "b@n.test", "accept_terms": "on", "website": ""})
        self.assertContains(response, "could not send the confirmation email", status_code=503)
        self.assertNotContains(response, "We sent a confirmation link", status_code=503)
        self.assertIn("SMTPAuthenticationError", logs.output[0])
        self.assertTrue(AuditEntry.objects.filter(action="email.failed", ok=False).exists())

    def test_an_invitation_that_cannot_be_sent_is_reported_to_the_super_user(self):
        client = Client()
        client.force_login(self.admin)
        with BROKEN, self.assertLogs("pgdas.mail", "ERROR"):
            response = client.post(reverse("employee_add"), {"name": "N", "designation": "", "email": "n@o.test", "role": "viewer"}, follow=True)
        self.assertContains(response, "could not be sent")

    def test_the_verification_page_reports_a_failure_and_lets_them_try_again_at_once(self):
        person = get_user_model().objects.create_user("old", email="old@o.test", password="pw-12345-abc")
        Membership.objects.create(user=person, organization=self.org, role="viewer")
        client = Client()
        with BROKEN, self.assertLogs("pgdas.mail", "ERROR"):
            client.post(reverse("login"), {"username": "old@o.test", "password": "pw-12345-abc"})
            response = client.post(reverse("email_verify_pending"), follow=True)
        self.assertContains(response, "could not be sent just now")
        mail.outbox = []
        response = client.post(reverse("email_verify_pending"), follow=True)            # nothing went out before, so no waiting period
        self.assertContains(response, "A new verification link was sent")
        self.assertEqual(len(mail.outbox), 1)