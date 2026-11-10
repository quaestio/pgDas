"""Dedicated and shared installations (PGDAS_MODE in .env), and registering an organization from the public page."""
import re
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.checks import run_checks
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEntry
from servers.models import ServerConnection

from . import mode
from .models import Installation, Membership, Organization, OrganizationServer

PASSWORD = "Sample-safe-9381!"
SHARED = dict(MODE="shared", ALLOW_ORGANIZATION_REGISTRATION=True)


class ModeFixture(TestCase):
    def setUp(self):
        cache.clear()
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="First")
        self.admin = get_user_model().objects.create_user("a", email="admin@first.test", password="pw-12345-abc", first_name="Ada")
        Membership.objects.create(user=self.admin, organization=self.org, role="super_user")

    def register(self, client=None, **extra):
        data = {"organization": "Second Org", "name": "Bea Boss", "designation": "Owner", "email": "bea@second.test", "password": PASSWORD,
                "password_confirm": PASSWORD, "accept_terms": "on", "website": "", **extra}
        return (client or Client()).post(reverse("register"), data)


class ModeSettingTests(TestCase):
    def test_dedicated_is_the_default_and_nothing_is_open(self):
        from django.conf import settings
        self.assertEqual((settings.MODE, settings.ALLOW_ORGANIZATION_REGISTRATION, settings.MAX_ORGANIZATIONS), ("dedicated", False, 0))

    def test_an_unknown_mode_stops_the_start_with_a_clear_message(self):
        import os, subprocess, sys
        from pathlib import Path
        from django.conf import settings
        result = subprocess.run([sys.executable, str(Path(settings.BASE_DIR) / "manage.py"), "check"], cwd=settings.BASE_DIR,
                                env=dict(os.environ, PGDAS_DEBUG="1", PGDAS_MODE="banana"), capture_output=True, text=True, timeout=120)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PGDAS_MODE must be dedicated or shared", result.stderr)


class DedicatedTests(ModeFixture):
    @override_settings(MODE="dedicated", ALLOW_ORGANIZATION_REGISTRATION=True)          # even with the switch on: dedicated means one
    def test_registration_does_not_exist(self):
        self.assertEqual(Client().get(reverse("register")).status_code, 404)
        self.assertEqual(self.register().status_code, 404)
        self.assertEqual(Organization.objects.count(), 1)
        self.assertNotContains(Client().get(reverse("login")), "Register your organization")

    @override_settings(MODE="dedicated")
    def test_creating_an_organization_is_refused_even_for_an_organization_that_allows_it(self):
        Organization.objects.filter(pk=self.org.pk).update(restrict_creation=False)
        client = Client()
        client.force_login(self.admin)
        response = client.get(reverse("organization_create"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(client.post(reverse("organization_create"), {"organization": "X"}).status_code, 403)

    @override_settings(MODE="dedicated")
    def test_the_settings_page_has_no_creation_switch_and_says_the_installation_is_dedicated(self):
        client = Client()
        client.force_login(self.admin)
        page = client.get(reverse("organization_edit"))
        self.assertNotContains(page, "restrict_creation")
        self.assertContains(page, "dedicated to one organization")
        self.assertNotContains(page, "Create another organization")

    @override_settings(MODE="dedicated")
    def test_the_header_shows_no_organization_name(self):
        client = Client()
        client.force_login(self.admin)
        self.assertNotContains(client.get("/"), "· First")
        self.assertNotContains(client.get("/"), "<small>First</small>")

    @override_settings(MODE="dedicated")
    def test_several_organizations_in_a_dedicated_installation_are_warned_about_and_cannot_grow(self):
        self.assertEqual([w for w in run_checks() if w.id == "access_control.W003"], [])
        Organization.objects.create(name="Leftover")
        warnings = [w for w in run_checks() if w.id == "access_control.W003"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("2 organizations", warnings[0].msg)
        self.assertTrue(mode.too_many_for_dedicated())
        client = Client()
        client.force_login(self.admin)
        self.assertContains(client.get(reverse("organization_edit")), "WARNING: PGDAS_MODE is dedicated but 2 organizations exist")

    @override_settings(MODE="dedicated")
    def test_people_still_join_the_one_organization_through_employees(self):
        client = Client()
        client.force_login(self.admin)
        response = client.post(reverse("employee_add"), {"name": "New Person", "designation": "", "email": "new@first.test", "role": "viewer",
                                                          "is_active": "on", "password": PASSWORD, "password_confirm": PASSWORD})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Membership.objects.filter(organization=self.org).count(), 2)


@override_settings(**SHARED)
class SharedRegistrationTests(ModeFixture):
    def test_the_page_and_the_sign_in_link_exist_only_when_the_switch_is_on(self):
        self.assertEqual(Client().get(reverse("register")).status_code, 200)
        self.assertContains(Client().get(reverse("login")), "Register your organization")
        with override_settings(ALLOW_ORGANIZATION_REGISTRATION=False):
            self.assertEqual(Client().get(reverse("register")).status_code, 404)
            self.assertNotContains(Client().get(reverse("login")), "Register your organization")

    def test_registering_creates_the_organization_and_its_first_super_user_who_can_sign_in(self):
        response = self.register()
        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
        organization = Organization.objects.get(name="Second Org")
        self.assertEqual((organization.active, organization.restrict_creation), (True, True))
        member = Membership.objects.get(organization=organization)
        self.assertEqual((member.role, member.user.email, member.user.is_active), ("super_user", "bea@second.test", True))
        self.assertTrue(Client().login(username="bea@second.test", password=PASSWORD))
        entry = AuditEntry.objects.get(action="organization.register")
        self.assertFalse(entry.payload["pending"])

    def test_the_organizations_are_completely_separate(self):
        self.register()
        bea = Client()
        bea.login(username="bea@second.test", password=PASSWORD)
        theirs = bea.get(reverse("employee_list"))
        self.assertContains(theirs, "bea@second.test")
        self.assertNotContains(theirs, "admin@first.test")
        mine = Client()
        mine.force_login(self.admin)
        self.assertNotIn("bea@second.test", [m.user.email for m in mine.get(reverse("employee_list")).context["people"]])

    def test_an_address_in_use_a_weak_password_a_mismatch_and_a_filled_hidden_field_register_nothing(self):
        for extra in ({"email": "ADMIN@first.test"}, {"password": "123", "password_confirm": "123"}, {"password_confirm": "other"}, {"website": "http://spam.test"}):
            with self.subTest(extra=extra):
                self.assertEqual(self.register(**extra).status_code, 200)
        self.assertEqual((Organization.objects.count(), get_user_model().objects.count()), (1, 1))

    @override_settings(REGISTRATIONS_PER_HOUR=2)
    def test_attempts_are_limited_per_address(self):
        statuses = [self.register(password="1").status_code for _ in range(3)]
        self.assertEqual(statuses, [200, 200, 429])
        self.assertContains(self.register(), "Too many attempts", status_code=429)
        elsewhere = Client(REMOTE_ADDR="198.51.100.7")
        self.assertEqual(self.register(elsewhere, password="1").status_code, 200)             # another address has its own count

    @override_settings(MAX_ORGANIZATIONS=2)
    def test_the_organization_limit_counts_everything_and_closes_the_page(self):
        self.assertEqual(self.register().status_code, 302)
        self.assertEqual(Client().get(reverse("register")).status_code, 404)
        self.assertNotContains(Client().get(reverse("login")), "Register your organization")
        self.assertEqual(self.register(organization="Third", email="c@third.test").status_code, 404)
        self.assertEqual(Organization.objects.count(), 2)

    @override_settings(MAX_ORGANIZATIONS=1)
    def test_the_limit_also_stops_a_super_user_from_creating_one(self):
        Organization.objects.filter(pk=self.org.pk).update(restrict_creation=False)
        client = Client()
        client.force_login(self.admin)
        self.assertEqual(client.get(reverse("organization_create")).status_code, 403)          # (the button is not shown either)
        self.assertEqual(client.post(reverse("organization_create"), {"organization": "X"}).status_code, 403)
        self.assertEqual(Organization.objects.count(), 1)
        self.assertNotContains(client.get(reverse("organization_edit")), "Create another organization")

    def test_someone_who_is_signed_in_is_sent_home(self):
        client = Client()
        client.force_login(self.admin)
        self.assertRedirects(client.get(reverse("register")), reverse("server_list"), fetch_redirect_response=False)

    def test_the_header_names_the_organization_in_a_shared_installation(self):
        client = Client()
        client.force_login(self.admin)
        self.assertContains(client.get("/"), "<small>First</small>")

    def test_registration_waits_until_setup_has_made_the_first_organization(self):
        Installation.objects.filter(pk=1).update(completed=False)
        self.assertRedirects(Client().get(reverse("register")), reverse("setup"), fetch_redirect_response=False)

    def test_an_existing_super_user_can_still_create_one_when_their_organization_allows_it(self):
        Organization.objects.filter(pk=self.org.pk).update(restrict_creation=False)
        client = Client()
        client.force_login(self.admin)
        self.assertEqual(client.get(reverse("organization_create")).status_code, 200)


@override_settings(EMAIL_HOST="smtp.test", EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", **SHARED)
class ConfirmationTests(ModeFixture):
    def registered(self):
        self.assertEqual(self.register().status_code, 200)                       # the "check your email" page, not a redirect
        link = re.search(r"http://\S+/register/confirm/\S+/", mail.outbox[-1].body).group(0)
        return link.replace("http://testserver", "")

    def test_the_organization_and_person_wait_until_the_emailed_link_is_opened(self):
        link = self.registered()
        organization = Organization.objects.get(name="Second Org")
        user = get_user_model().objects.get(email="bea@second.test")
        self.assertEqual((organization.active, user.is_active), (False, False))
        self.assertEqual(mail.outbox[-1].to, ["bea@second.test"])
        self.assertFalse(Client().login(username="bea@second.test", password=PASSWORD))         # not before the link
        response = Client().get(link)
        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
        organization.refresh_from_db(); user.refresh_from_db()
        self.assertEqual((organization.active, user.is_active), (True, True))
        self.assertTrue(Client().login(username="bea@second.test", password=PASSWORD))
        self.assertTrue(AuditEntry.objects.filter(action="organization.confirm").exists())

    def test_opening_the_link_twice_changes_nothing_more(self):
        link = self.registered()
        Client().get(link)
        again = Client().get(link, follow=True)
        self.assertContains(again, "already confirmed")
        self.assertEqual(AuditEntry.objects.filter(action="organization.confirm").count(), 1)

    def test_a_forged_a_damaged_and_an_expired_link_are_refused(self):
        link = self.registered()
        for bad in (link[:-3] + "x/", "/register/confirm/not-a-token/", link.replace("confirm/", "confirm/x")):
            self.assertEqual(Client().get(bad).status_code, 400, bad)
        with override_settings(REGISTRATION_CONFIRM_HOURS=0):
            response = Client().get(link)
            self.assertContains(response, "not valid or has expired", status_code=400)
        self.assertFalse(Organization.objects.get(name="Second Org").active)

    @override_settings(MAX_ORGANIZATIONS=2)
    def test_an_unconfirmed_organization_already_counts_towards_the_limit(self):
        self.registered()
        self.assertEqual(Client().get(reverse("register")).status_code, 404)

@override_settings(**SHARED)
class LoginPageRegistrationTests(ModeFixture):
    def page(self, **kwargs):
        return Client().get(reverse("login"), **kwargs)

    def test_the_sign_in_page_carries_the_registration_with_cautions_and_the_terms(self):
        page = self.page()
        for needle in ('id="tabSignin"', 'id="tabRegister"', "Register your organization", 'id="panelRegister"', 'action="/register/"', "Before you register",
                       "first Super User", "least-privilege", "email address is your sign-in ID", 'name="accept_terms"', 'name="organization"', 'name="email"',
                       'name="password"', 'name="password_confirm"', 'href="/terms/"', "login_tabs.js", "register.css"):
            self.assertContains(page, needle)
        self.assertContains(page, 'name="website"')                          # the hidden field is in the form but off screen
        self.assertContains(page, "Authorised use only")
        self.assertEqual(page.context["register_form"].fields["accept_terms"].required, True)

    def test_without_registration_the_page_has_only_sign_in_and_still_the_caution(self):
        with override_settings(ALLOW_ORGANIZATION_REGISTRATION=False):
            page = self.page()
        for needle in ("tabRegister", "panelRegister", "Before you register", "accept_terms", "Register your organization"):
            self.assertNotContains(page, needle)
        self.assertContains(page, "Authorised use only")
        self.assertContains(page, 'name="username"')

    def test_the_terms_must_be_accepted_or_nothing_is_registered(self):
        data = {"organization": "Second Org", "name": "Bea Boss", "designation": "", "email": "bea@second.test", "password": PASSWORD,
                "password_confirm": PASSWORD, "website": ""}
        response = Client().post(reverse("register"), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You must accept the Terms and Conditions to register.")
        self.assertContains(response, 'id="panelRegister"')
        self.assertEqual((Organization.objects.count(), get_user_model().objects.count()), (1, 1))

    def test_what_was_accepted_is_recorded_with_the_version_the_time_and_the_address(self):
        self.register(Client(REMOTE_ADDR="203.0.113.5"))
        terms = AuditEntry.objects.get(action="organization.register").payload["terms"]
        from accounts import terms as terms_module
        self.assertEqual((terms["version"], terms["url"], terms["address"]), (terms_module.VERSION, "/terms/", "203.0.113.5"))
        self.assertRegex(terms["accepted_at"], r"^\d{4}-\d\d-\d\dT")

    def test_the_register_address_shows_the_sign_in_page_with_the_register_tab_selected(self):
        page = Client().get(reverse("register"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'data-active="register"')
        self.assertTemplateUsed(page, "registration/login.html")

    def test_mistakes_come_back_on_the_same_page_with_the_register_tab_selected_and_what_was_typed(self):
        response = self.register(password_confirm="different", organization="Kept Name")
        self.assertContains(response, 'data-active="register"')
        self.assertContains(response, "Passwords do not match.")
        self.assertContains(response, "Kept Name")

    def test_your_own_terms_address_replaces_the_built_in_page(self):
        with override_settings(TERMS_URL="https://example.test/our-terms"):
            page = self.page()
            self.assertContains(page, 'href="https://example.test/our-terms"')
            self.assertNotContains(page, 'href="/terms/"')
            self.register()
        self.assertEqual(AuditEntry.objects.get(action="organization.register").payload["terms"]["url"], "https://example.test/our-terms")

    def test_the_built_in_terms_can_be_read_signed_out_or_in_and_say_what_they_cover(self):
        out = Client().get(reverse("terms"))
        self.assertEqual(out.status_code, 200)
        for needle in ("Terms and Conditions", "Version 2026-11-10", "Acceptable use", "Changes you make are real", "audit log", "no warranty", "not legal advice"):
            self.assertContains(out, needle)
        signed = Client()
        signed.force_login(self.admin)
        self.assertContains(signed.get(reverse("terms")), "Acceptable use")

    def test_the_terms_page_is_not_a_way_around_setup(self):
        Installation.objects.filter(pk=1).update(completed=False)
        self.assertRedirects(Client().get(reverse("terms")), reverse("setup"), fetch_redirect_response=False)

    @override_settings(EMAIL_HOST="smtp.test", EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_the_check_your_email_message_is_on_the_same_page(self):
        response = self.register()
        self.assertContains(response, "We sent a confirmation link to <strong>bea@second.test</strong>")
        self.assertNotContains(response, 'name="accept_terms"')

    def test_the_tab_script_handles_clicks_keys_and_the_hash(self):
        from pathlib import Path
        from django.conf import settings
        script = (Path(settings.BASE_DIR) / "static" / "login_tabs.js").read_text(encoding="utf-8")
        for needle in ('role="tab"', "aria-selected", "ArrowRight", '"#register"', "panel.hidden"):
            self.assertIn(needle, script)
        self.assertNotIn("innerHTML", script)


class HostCommandTests(ModeFixture):
    def run_command(self, *args):
        out = StringIO()
        call_command("organizations", *args, stdout=out)
        return out.getvalue()

    def test_the_list_shows_every_organization_with_its_counts(self):
        server = ServerConnection.objects.create(name="S", host="h", username="u")
        OrganizationServer.objects.create(server=server, organization=self.org)
        Organization.objects.create(name="Empty", active=False)
        text = self.run_command()
        self.assertRegex(text, r"First")
        self.assertRegex(text, r"\s1\s+1\s+1\s+First")                           # people, super users, servers
        self.assertRegex(text, r"NO\s+0\s+0\s+0\s+Empty")

    def test_switching_an_organization_off_stops_its_people_and_on_lets_them_back(self):
        client = Client()
        client.force_login(self.admin)
        self.assertEqual(client.get("/").status_code, 200)
        self.run_command("--disable", "First")
        self.assertEqual(client.get("/").status_code, 403)
        self.assertTrue(AuditEntry.objects.filter(action="organization.disable").exists())
        self.run_command("--enable", "First")
        self.assertEqual(client.get("/").status_code, 200)
        with self.assertRaisesMessage(CommandError, "no organization named"):
            self.run_command("--disable", "Nope")

    def test_old_unconfirmed_registrations_are_removed_and_nothing_else(self):
        pending = Organization.objects.create(name="Pending", active=False)
        old = get_user_model().objects.create_user("p", email="p@pending.test", password="x", is_active=False)
        get_user_model().objects.filter(pk=old.pk).update(date_joined=timezone.now() - timedelta(hours=100))
        Membership.objects.create(user=old, organization=pending, role="super_user")
        recent = Organization.objects.create(name="Recent", active=False)
        young = get_user_model().objects.create_user("r", email="r@recent.test", password="x", is_active=False)
        Membership.objects.create(user=young, organization=recent, role="super_user")
        self.assertIn("1 unconfirmed", self.run_command("--purge-pending", "48"))
        self.assertEqual(sorted(Organization.objects.values_list("name", flat=True)), ["First", "Recent"])
        self.assertFalse(get_user_model().objects.filter(email="p@pending.test").exists())
        self.assertTrue(get_user_model().objects.filter(email="admin@first.test").exists())