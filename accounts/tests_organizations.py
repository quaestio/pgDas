"""Organization settings, creating another organization with its first Super User, and the repair for one that has none."""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.checks import run_checks
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from access_control.models import Installation, Membership, Organization
from audit.models import AuditEntry

PASSWORD = "Sample-safe-9381!"


@override_settings(MODE="shared")                # creating organizations and the creation switch exist only on a shared installation
class OrganizationFixture(TestCase):
    def setUp(self):
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Team A")
        User = get_user_model()
        self.admin = User.objects.create_user("a", email="admin@example.test", password="pw-12345-abc")
        Membership.objects.create(user=self.admin, organization=self.org, role="super_user")
        self.editor = User.objects.create_user("e", email="editor@example.test", password="pw-12345-abc")
        Membership.objects.create(user=self.editor, organization=self.org, role="editor")
        self.client = Client()
        self.client.force_login(self.admin)

    def settings_post(self, **extra):
        data = {"name": "Team A", "allowed_ips": "", **extra}
        return self.client.post(reverse("organization_edit"), data)

    def new_organization(self, **extra):
        data = {"organization": "Team B", "name": "Bea Boss", "designation": "Owner", "email": "bea@example.test", "password": PASSWORD,
                "password_confirm": PASSWORD, **extra}
        return self.client.post(reverse("organization_create"), data)


class SettingsTests(OrganizationFixture):
    def test_creation_is_restricted_by_default_and_the_list_is_empty(self):
        self.assertEqual((self.org.restrict_creation, self.org.allowed_ips), (True, ""))

    def test_a_super_user_saves_the_settings_and_the_change_is_recorded(self):
        response = self.settings_post(restrict_creation="", allowed_ips="10.0.0.9/24, 203.0.113.7\n# office")
        self.assertRedirects(response, reverse("employee_list"), fetch_redirect_response=False)
        self.org.refresh_from_db()
        self.assertEqual((self.org.restrict_creation, self.org.allowed_ips), (False, "10.0.0.0/24\n203.0.113.7"))
        entry = AuditEntry.objects.get(action="organization.edit")
        self.assertEqual(set(entry.payload["changed"]), {"restrict_creation", "allowed_ips"})
        self.assertEqual(entry.payload["changed"]["restrict_creation"], {"from": True, "to": False})

    def test_bad_entries_are_refused_with_their_names_and_nothing_is_saved(self):
        response = self.settings_post(restrict_creation="on", allowed_ips="10.0.0.1\nnope\n999.1.1.1")
        self.assertContains(response, "nope, 999.1.1.1")
        self.org.refresh_from_db()
        self.assertEqual(self.org.allowed_ips, "")

    def test_the_page_shows_your_address_and_that_super_users_are_exempt(self):
        page = self.client.get(reverse("organization_edit"))
        self.assertContains(page, "Your address now is 127.0.0.1")
        self.assertContains(page, "Super Users can")
        self.assertNotContains(page, "Create another organization")                       # creation is restricted

    def test_only_super_users_may_open_the_settings(self):
        other = Client()
        other.force_login(self.editor)
        self.assertEqual(other.get(reverse("organization_edit")).status_code, 403)
        self.assertEqual(other.post(reverse("organization_edit"), {"name": "Hacked", "allowed_ips": "1.1.1.1"}).status_code, 403)
        self.org.refresh_from_db()
        self.assertEqual((self.org.name, self.org.allowed_ips), ("Team A", ""))


class CreateOrganizationTests(OrganizationFixture):
    def test_when_creation_is_restricted_the_form_and_the_post_are_refused(self):
        self.assertEqual(self.client.get(reverse("organization_create")).status_code, 403)
        self.assertEqual(self.new_organization().status_code, 403)
        self.assertFalse(Organization.objects.filter(name="Team B").exists())

    def test_when_allowed_it_creates_the_organization_and_its_first_super_user_together(self):
        Organization.objects.filter(pk=self.org.pk).update(restrict_creation=False)
        page = self.client.get(reverse("organization_edit"))
        self.assertContains(page, "Create another organization")
        self.assertEqual(self.client.get(reverse("organization_create")).status_code, 200)
        response = self.new_organization()
        self.assertRedirects(response, reverse("organization_edit"), fetch_redirect_response=False)
        organization = Organization.objects.get(name="Team B")
        self.assertTrue(organization.restrict_creation)                                    # the new one starts restricted too
        member = Membership.objects.get(organization=organization)
        self.assertEqual((member.role, member.user.email, member.user.is_active), ("super_user", "bea@example.test", True))
        self.assertTrue(Client().login(username="bea@example.test", password=PASSWORD))
        entry = AuditEntry.objects.get(action="organization.create")
        self.assertEqual(entry.payload["organization"], organization.pk)

    def test_the_creator_sees_nothing_of_the_new_organization_and_the_new_super_user_nothing_of_theirs(self):
        Organization.objects.filter(pk=self.org.pk).update(restrict_creation=False)
        self.new_organization()
        listing = self.client.get(reverse("employee_list"))
        self.assertNotIn("bea@example.test", [person.user.email for person in listing.context["people"]])
        bea = Client()
        bea.login(username="bea@example.test", password=PASSWORD)
        theirs = bea.get(reverse("employee_list"))
        self.assertContains(theirs, "bea@example.test")
        self.assertNotContains(theirs, "admin@example.test")
        self.assertEqual(bea.get(reverse("organization_create")).status_code, 403)         # restricted until their own Super User changes it

    def test_an_address_already_in_use_anywhere_is_refused_and_nothing_is_created(self):
        Organization.objects.filter(pk=self.org.pk).update(restrict_creation=False)
        for address in ("editor@example.test", "EDITOR@Example.Test"):
            response = self.new_organization(email=address)
            self.assertContains(response, "Another employee already uses this email address.")
        self.assertEqual(Organization.objects.count(), 1)

    def test_a_weak_password_or_a_mismatch_creates_nothing(self):
        Organization.objects.filter(pk=self.org.pk).update(restrict_creation=False)
        self.new_organization(password="123", password_confirm="123")
        self.new_organization(password_confirm="different")
        self.assertEqual((Organization.objects.count(), get_user_model().objects.count()), (1, 2))

    def test_a_failure_while_saving_leaves_neither_the_organization_nor_the_person(self):
        from unittest.mock import patch
        Organization.objects.filter(pk=self.org.pk).update(restrict_creation=False)
        with patch("accounts.views.Membership.objects.create", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.new_organization()
        self.assertEqual((Organization.objects.count(), get_user_model().objects.count()), (1, 2))

    def test_only_a_super_user_may_create_one(self):
        Organization.objects.filter(pk=self.org.pk).update(restrict_creation=False)
        other = Client()
        other.force_login(self.editor)
        self.assertEqual(other.get(reverse("organization_create")).status_code, 403)


class SuperUserRepairTests(OrganizationFixture):
    def lonely(self):
        organization = Organization.objects.create(name="Lonely")
        person = get_user_model().objects.create_user("v", email="viewer@lonely.test", password="pw-12345-abc")
        Membership.objects.create(user=person, organization=organization, role="viewer")
        return organization

    def run_command(self, *args):
        out = StringIO()
        call_command("create_superuser", *args, stdout=out)
        return out.getvalue()

    def test_the_check_warns_about_an_organization_without_an_active_super_user(self):
        self.assertEqual([w for w in run_checks() if w.id == "access_control.W001"], [])
        self.lonely()
        warnings = [w for w in run_checks() if w.id == "access_control.W001"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("Lonely", warnings[0].msg)
        self.assertIn("create_superuser", warnings[0].hint)

    def test_a_deactivated_super_user_does_not_count(self):
        get_user_model().objects.filter(pk=self.admin.pk).update(is_active=False)
        self.assertEqual(len([w for w in run_checks() if w.id == "access_control.W001"]), 1)

    def test_the_command_creates_a_super_user_for_it(self):
        organization = self.lonely()
        text = self.run_command("--organization", "Lonely", "--email", "Boss@Lonely.Test", "--name", "Boss", "--password", PASSWORD)
        self.assertIn("created", text)
        member = Membership.objects.get(organization=organization, role="super_user")
        self.assertEqual((member.user.email, member.user.is_superuser, member.user.is_active), ("boss@lonely.test", True, True))
        self.assertTrue(Client().login(username="boss@lonely.test", password=PASSWORD))
        self.assertEqual([w for w in run_checks() if w.id == "access_control.W001"], [])
        self.assertTrue(AuditEntry.objects.filter(action="superuser.create").exists())

    def test_it_refuses_an_organization_that_has_one_unless_forced_and_bad_input(self):
        base = ["--organization", "Team A", "--name", "X", "--password", PASSWORD]
        with self.assertRaisesMessage(CommandError, "already has an active Super User"):
            self.run_command(*base, "--email", "x@example.test")
        self.run_command(*base, "--email", "x@example.test", "--force")
        with self.assertRaisesMessage(CommandError, "no organization named"):
            self.run_command("--organization", "Nope", "--email", "y@example.test", "--name", "Y", "--password", PASSWORD)
        self.lonely()
        for email, password, message in (("not-an-address", PASSWORD, "not a valid email"), ("editor@example.test", PASSWORD, "already uses"),
                                         ("z@example.test", "123", "too short")):
            with self.subTest(email=email):
                with self.assertRaises(CommandError) as caught:
                    self.run_command("--organization", "Lonely", "--email", email, "--name", "Z", "--password", password)
                self.assertIn(message, str(caught.exception))
        self.assertFalse(Membership.objects.filter(organization__name="Lonely", role="super_user").exists())
