"""Email is the one sign-in ID: unique across the installation, whatever the capital letters, and there is no username to sign in with."""
import importlib
from io import StringIO

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse

from access_control.models import Installation, Membership, Organization

migration = importlib.import_module("accounts.migrations.0004_unique_email")


class FakeSchemaEditor:
    class connection:
        alias = "default"


class EmailSignInTests(TestCase):
    def setUp(self):
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Team")
        self.user = get_user_model().objects.create_user("ignored", email="Pat@Example.Test", password="pw-12345-abc")
        Membership.objects.create(user=self.user, organization=self.org, role="viewer")

    def sign_in(self, identifier, password="pw-12345-abc"):
        return Client().post(reverse("login"), {"username": identifier, "password": password})

    def test_the_address_is_stored_lower_case_and_becomes_the_internal_username(self):
        self.user.refresh_from_db()
        self.assertEqual((self.user.email, self.user.username), ("pat@example.test", "pat@example.test"))

    def test_sign_in_with_the_address_in_any_case_and_with_spaces(self):
        for identifier in ("pat@example.test", "PAT@EXAMPLE.TEST", "  Pat@Example.test "):
            with self.subTest(identifier=identifier):
                response = self.sign_in(identifier)
                self.assertEqual(response.status_code, 302, identifier)

    def test_a_username_or_a_wrong_password_does_not_sign_in(self):
        for identifier, password, message in (("ignored", "pw-12345-abc", "Enter a valid email address."), ("pat", "pw-12345-abc", "Enter a valid email address."),
                                              ("pat@example.test", "wrong", "Enter a correct email address and password."),
                                              ("nobody@example.test", "pw-12345-abc", "Enter a correct email address and password.")):
            with self.subTest(identifier=identifier, password=password):
                response = self.sign_in(identifier, password)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, message)

    def test_the_login_page_asks_for_an_email_not_a_username(self):
        page = Client().get(reverse("login")).content.decode()
        self.assertIn('type="email"', page)
        self.assertIn(">EMAIL<", page)
        self.assertNotIn(">USERNAME", page.upper())

    def test_an_account_without_an_address_cannot_sign_in(self):
        get_user_model().objects.create_user("legacy-name", password="pw-12345-abc")
        for identifier in ("legacy-name", "legacy-name@example.test"):
            self.assertEqual(self.sign_in(identifier).status_code, 200)               # (the first is not an address; the second matches nobody)

    def test_two_accounts_cannot_share_an_address_whatever_the_capitals(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            get_user_model().objects.create_user("other", email="PAT@example.test", password="x")
        self.assertEqual(get_user_model().objects.filter(email="pat@example.test").count(), 1)

    def test_several_accounts_may_have_no_address_yet(self):
        User = get_user_model()
        User.objects.create_user("one", password="x")
        User.objects.create_user("two", password="x")
        self.assertEqual(User.objects.filter(email="").count(), 2)

    def test_changing_the_address_updates_the_internal_username(self):
        self.user.email = "New.Name@Example.Test"
        self.user.save(update_fields=["email"])
        self.user.refresh_from_db()
        self.assertEqual((self.user.email, self.user.username), ("new.name@example.test", "new.name@example.test"))
        self.assertEqual(self.sign_in("new.name@example.test").status_code, 302)
        self.assertEqual(self.sign_in("pat@example.test").status_code, 200)

    def test_the_report_lists_accounts_without_an_address(self):
        get_user_model().objects.create_user("legacy-name", password="x")
        out = StringIO()
        call_command("accounts_without_email", stdout=out)
        self.assertIn("legacy-name", out.getvalue())
        self.assertIn("1 account(s) without an email address.", out.getvalue())
        self.assertNotIn("pat@example.test", out.getvalue())


class MigrationTests(TestCase):
    def run_migration(self):
        migration.normalise(apps, FakeSchemaEditor())

    def test_addresses_are_lowered_and_a_username_that_is_an_address_becomes_the_email(self):
        User = get_user_model()
        a = User.objects.create_user("Sam@Example.Test", password="x")
        User.objects.filter(pk=a.pk).update(email="", username="Sam@Example.Test")
        b = User.objects.create_user("b-name", password="x")
        User.objects.filter(pk=b.pk).update(email=" Bea@Example.Test ", username="b-name")
        c = User.objects.create_user("no-address", password="x")
        self.run_migration()
        for user in (a, b, c):
            user.refresh_from_db()
        self.assertEqual((a.email, a.username), ("sam@example.test", "sam@example.test"))
        self.assertEqual((b.email, b.username), ("bea@example.test", "bea@example.test"))
        self.assertEqual((c.email, c.username), ("", "no-address"))

    def test_two_accounts_that_would_share_an_address_stop_the_migration_with_a_list(self):
        User = get_user_model()
        first = User.objects.create_user("first", email="dup@example.test", password="x")
        second = User.objects.create_user("Dup@Example.Test", password="x")
        User.objects.filter(pk=second.pk).update(email="", username="Dup@Example.Test")
        with self.assertRaises(RuntimeError) as caught:
            self.run_migration()
        message = str(caught.exception)
        self.assertIn("dup@example.test", message)
        self.assertIn(f"#{first.pk}", message)
        self.assertIn(f"#{second.pk}", message)
