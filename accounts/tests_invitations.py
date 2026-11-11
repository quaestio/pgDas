"""Inviting employees: an emailed link confirms the address and lets the person choose their own password."""
import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from access_control.models import Installation, Membership, Organization
from audit.models import AuditEntry

PASSWORD = "Sample-safe-9381!"
EMAIL = dict(EMAIL_HOST="smtp.test", EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")


@override_settings(**EMAIL)
class InvitationTests(TestCase):
    def setUp(self):
        mail.outbox = []
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Org")
        self.admin = get_user_model().objects.create_user("a", email="admin@org.test", password="pw-12345-abc", is_staff=True, is_superuser=True)
        Membership.objects.create(user=self.admin, organization=self.org, role="super_user")
        self.client = Client()
        self.client.force_login(self.admin)

    def invite(self, email="new@org.test", role="viewer"):
        return self.client.post(reverse("employee_add"), {"name": "New Person", "designation": "", "email": email, "role": role})

    def link(self):
        return re.search(r"http://\S+/invitation/\S+/", mail.outbox[-1].body).group(0).replace("http://testserver", "")

    def test_the_form_asks_for_no_password_and_no_active_switch(self):
        page = self.client.get(reverse("employee_add"))
        self.assertNotContains(page, 'name="password"')
        self.assertNotContains(page, 'name="is_active"')
        self.assertContains(page, "A link to confirm it")

    def test_inviting_makes_a_waiting_person_and_emails_the_link(self):
        self.assertEqual(self.invite().status_code, 302)
        user = get_user_model().objects.get(email="new@org.test")
        self.assertEqual((user.is_active, user.has_usable_password()), (False, False))
        self.assertEqual(Membership.objects.get(user=user).organization, self.org)
        self.assertEqual(mail.outbox[-1].to, ["new@org.test"])
        self.assertFalse(Client().login(username="new@org.test", password=PASSWORD))

    def test_the_person_chooses_a_password_from_the_link_and_can_then_sign_in(self):
        self.invite()
        page = Client().get(self.link())
        self.assertContains(page, 'name="new_password1"')
        self.assertFalse(get_user_model().objects.get(email="new@org.test").is_active)        # opening the link changes nothing
        response = Client().post(self.link(), {"new_password1": PASSWORD, "new_password2": PASSWORD})
        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
        self.assertTrue(Client().login(username="new@org.test", password=PASSWORD))
        self.assertTrue(AuditEntry.objects.filter(action="employee.accept").exists())

    def test_the_link_works_once_and_a_forged_or_expired_one_is_refused(self):
        self.invite()
        link = self.link()
        Client().post(link, {"new_password1": PASSWORD, "new_password2": PASSWORD})
        again = Client().post(link, {"new_password1": "Another-safe-7712!", "new_password2": "Another-safe-7712!"}, follow=True)
        self.assertContains(again, "already used")
        self.assertFalse(Client().login(username="new@org.test", password="Another-safe-7712!"))
        self.assertEqual(Client().get(link[:-3] + "x/").status_code, 400)
        with override_settings(INVITATION_HOURS=0):
            self.assertEqual(Client().get(link).status_code, 400)

    def test_an_invited_super_user_does_not_count_until_they_accept(self):
        self.invite("boss@org.test", role="super_user")
        self.assertFalse(Membership.objects.filter(organization=self.org, role="super_user", user__is_active=True).exclude(user=self.admin).exists())

    def test_the_invitation_can_be_sent_again(self):
        self.invite()
        user = get_user_model().objects.get(email="new@org.test")
        page = self.client.get(reverse("employee_edit", args=[user.pk]))
        self.assertContains(page, "Send the invitation email again")
        self.client.post(reverse("employee_edit", args=[user.pk]), {"name": "New Person", "designation": "", "email": "new@org.test", "role": "viewer",
                                                                  "resend_invitation": "on"})
        self.assertEqual(len(mail.outbox), 2)

    def test_an_address_in_use_is_refused(self):
        self.assertEqual(self.invite("ADMIN@org.test").status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_HOST="")
    def test_without_email_the_administrator_still_sets_the_password(self):
        page = self.client.get(reverse("employee_add"))
        self.assertContains(page, 'name="password"')
        response = self.client.post(reverse("employee_add"), {"name": "N", "designation": "", "email": "n@org.test", "role": "viewer", "is_active": "on",
                                                              "password": PASSWORD, "password_confirm": PASSWORD})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Client().login(username="n@org.test", password=PASSWORD))