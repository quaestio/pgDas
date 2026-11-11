"""Verifying the email address of accounts that never proved it."""
import re
from io import StringIO

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from access_control.models import Installation, Membership, Organization
from audit.models import AuditEntry

PASSWORD = "Sample-safe-9381!"
EMAIL = dict(EMAIL_HOST="smtp.test", EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")


@override_settings(**EMAIL)
class VerificationTests(TestCase):
    def setUp(self):
        cache.clear()
        mail.outbox = []
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        org = Organization.objects.create(name="Org")
        self.user = get_user_model().objects.create_user("old", email="old@org.test", password=PASSWORD)          # an account from before: unverified
        Membership.objects.create(user=self.user, organization=org, role="viewer")

    def sign_in(self, client=None):
        return (client or Client()).post(reverse("login"), {"username": "old@org.test", "password": PASSWORD})

    def link(self):
        return re.search(r"http://\S+/verify-email/\S+/", mail.outbox[-1].body).group(0).replace("http://testserver", "")

    def test_an_unverified_account_is_refused_and_sent_a_link_even_with_the_right_password(self):
        response = self.sign_in()
        self.assertContains(response, "Your email address is not verified yet")
        self.assertNotIn("_auth_user_id", response.wsgi_request.session)
        self.assertEqual(mail.outbox[-1].to, ["old@org.test"])

    def test_the_link_is_not_sent_again_within_a_few_minutes(self):
        self.sign_in()
        again = self.sign_in()
        self.assertContains(again, "sent a moment ago")
        self.assertEqual(len(mail.outbox), 1)

    def test_a_wrong_password_never_sends_anything(self):
        Client().post(reverse("login"), {"username": "old@org.test", "password": "wrong"})
        self.assertEqual(len(mail.outbox), 0)

    def test_opening_the_link_shows_a_button_and_only_the_button_verifies(self):
        self.sign_in()
        link = self.link()
        self.assertContains(Client().get(link), "VERIFY")
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)
        self.assertRedirects(Client().post(link), reverse("login"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_verified)
        self.assertEqual(self.sign_in().status_code, 302)
        self.assertTrue(AuditEntry.objects.filter(action="account.email_verified").exists())

    def test_a_forged_a_expired_and_an_old_address_link_are_refused(self):
        self.sign_in()
        link = self.link()
        self.assertEqual(Client().get(link[:-3] + "x/").status_code, 400)
        with override_settings(INVITATION_HOURS=0):
            self.assertEqual(Client().get(link).status_code, 400)
        get_user_model().objects.filter(pk=self.user.pk).update(email="new@org.test")          # the address changed after the link was sent
        self.assertEqual(Client().post(link).status_code, 400)
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)

    def test_nothing_changes_without_email_configured(self):
        with override_settings(EMAIL_HOST=""):
            self.assertEqual(self.sign_in().status_code, 302)

    def test_changing_an_employees_email_makes_it_unverified_again(self):
        admin = get_user_model().objects.create_user("adm", email="adm@org.test", password="pw-12345-abc", is_staff=True, is_superuser=True, email_verified=True)
        Membership.objects.create(user=admin, organization=Organization.objects.get(name="Org"), role="super_user")
        get_user_model().objects.filter(pk=self.user.pk).update(email_verified=True)
        client = Client()
        client.force_login(admin)
        client.post(reverse("employee_edit", args=[self.user.pk]), {"name": "Old", "designation": "", "email": "changed@org.test", "role": "viewer", "is_active": "on"})
        self.user.refresh_from_db()
        self.assertEqual((self.user.email, self.user.email_verified), ("changed@org.test", False))

    def test_the_command_marks_one_address_or_all_verified(self):
        with self.assertRaises(Exception):
            call_command("verify_email", "nobody@org.test", stdout=StringIO())
        call_command("verify_email", "OLD@org.test", stdout=StringIO())
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_verified)
        get_user_model().objects.update(email_verified=False)
        call_command("verify_email", "--all", stdout=StringIO())
        self.assertFalse(get_user_model().objects.filter(email_verified=False).exists())