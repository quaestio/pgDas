"""Server address policy (SSRF block list) and sign-in throttling."""
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.checks import run_checks
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEntry
from backups import dump
from explorer import pg
from servers import netpolicy
from servers.models import ServerConnection

from .models import Installation, Membership, Organization, OrganizationServer, SignInFailure

PUBLIC = dict(SERVER_NETWORKS="public", ALLOWED_SERVER_HOSTS=[])


def fake_dns(mapping):
    def getaddrinfo(host, *args, **kwargs):
        if host not in mapping:
            import socket
            raise socket.gaierror
        return [(2, 1, 6, "", (mapping[host], 0))]
    return mock.patch("servers.netpolicy.socket.getaddrinfo", getaddrinfo)


class NetPolicyTests(TestCase):
    @override_settings(**PUBLIC)
    def test_private_local_and_metadata_addresses_are_refused_and_public_ones_allowed(self):
        for bad in ("127.0.0.1", "10.1.2.3", "192.168.0.5", "172.16.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1", "fe80::1", "fd00::1",
                    "::ffff:127.0.0.1", "[::1]"):
            with self.subTest(bad=bad), self.assertRaises(netpolicy.AddressNotAllowed):
                netpolicy.check(bad)
        self.assertEqual(netpolicy.check("93.184.216.34"), "93.184.216.34")

    @override_settings(**PUBLIC)
    def test_a_name_is_resolved_and_every_answer_must_be_public(self):
        with fake_dns({"db.example.test": "93.184.216.34", "inside.example.test": "10.0.0.8"}):
            self.assertEqual(netpolicy.check("db.example.test"), "93.184.216.34")
            with self.assertRaises(netpolicy.AddressNotAllowed):
                netpolicy.check("inside.example.test")
            with self.assertRaisesMessage(netpolicy.AddressNotAllowed, "does not resolve"):
                netpolicy.check("nothing.example.test")

    @override_settings(SERVER_NETWORKS="public", ALLOWED_SERVER_HOSTS=["localhost", "10.0.0.0/24", "192.168.1.9"])
    def test_the_allow_list_accepts_names_addresses_and_ranges_only(self):
        with fake_dns({"localhost": "127.0.0.1"}):
            self.assertEqual(netpolicy.check("localhost"), "127.0.0.1")
        self.assertEqual(netpolicy.check("10.0.0.77"), "10.0.0.77")
        self.assertEqual(netpolicy.check("192.168.1.9"), "192.168.1.9")
        for bad in ("10.0.1.1", "192.168.1.10", "127.0.0.1"):
            with self.subTest(bad=bad), self.assertRaises(netpolicy.AddressNotAllowed):
                netpolicy.check(bad)

    @override_settings(SERVER_NETWORKS="any")
    def test_any_allows_everything_and_pins_nothing(self):
        self.assertEqual(netpolicy.check("127.0.0.1"), "")

    def test_the_default_policy_follows_the_mode(self):
        with override_settings(MODE="shared", SERVER_NETWORKS=""):
            self.assertEqual(netpolicy.policy(), "public")
        with override_settings(MODE="dedicated", SERVER_NETWORKS=""):
            self.assertEqual(netpolicy.policy(), "any")
        with override_settings(MODE="shared", SERVER_NETWORKS="any"):
            self.assertEqual(netpolicy.policy(), "any")

    def test_a_host_must_be_only_a_host(self):
        for bad in ("http://x.test", "x.test/path", "x y", "-oProxy", "", "a;b"):
            with self.subTest(bad=bad), self.assertRaises(netpolicy.AddressNotAllowed):
                netpolicy.clean_host(bad)

    @override_settings(**PUBLIC)
    def test_a_name_that_changes_its_answer_is_caught_at_connection_and_the_checked_address_is_used(self):
        server = ServerConnection(name="S", host="db.example.test", port=5432, username="u")
        server.set_password("x") if hasattr(server, "set_password") else None
        with fake_dns({"db.example.test": "93.184.216.34"}), mock.patch("access_control.runtime.execution_server", lambda s, d: s), \
                mock.patch("explorer.pg.psycopg.connect") as connect, mock.patch.object(ServerConnection, "get_password", lambda self: "x"):
            pg._open(server, "postgres")
        self.assertEqual(connect.call_args.kwargs["hostaddr"], "93.184.216.34")
        with fake_dns({"db.example.test": "169.254.169.254"}), mock.patch("access_control.runtime.execution_server", lambda s, d: s), \
                mock.patch("explorer.pg.psycopg.connect") as connect:
            with self.assertRaises(pg.PgError):
                pg._open(server, "postgres")
        connect.assert_not_called()

    @override_settings(**PUBLIC)
    def test_pg_dump_gets_the_checked_address_and_is_refused_for_a_private_one(self):
        good = ServerConnection(name="S", host="93.184.216.34", port=5432, username="u")
        bad = ServerConnection(name="B", host="10.0.0.1", port=5432, username="u")
        with mock.patch.object(ServerConnection, "get_password", lambda self: "x"):
            self.assertEqual(dump._environment(good)["PGHOSTADDR"], "93.184.216.34")
            with self.assertRaises(pg.PgError):
                dump._environment(bad)


class ServerFormAndTestLimitTests(TestCase):
    def setUp(self):
        cache.clear()
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Org")
        self.user = get_user_model().objects.create_user("a", email="a@org.test", password="pw-12345-abc")
        Membership.objects.create(user=self.user, organization=self.org, role="super_user")
        self.client = Client()
        self.client.force_login(self.user)

    def add(self, host):
        return self.client.post(reverse("server_add"), {"name": "N", "host": host, "port": 5432, "username": "u", "password": "p", "sslmode": "prefer"})

    @override_settings(**PUBLIC)
    def test_saving_a_private_host_is_refused_in_the_form(self):
        response = self.add("10.0.0.5")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PGDAS_ALLOWED_SERVER_HOSTS")
        self.assertEqual(ServerConnection.objects.count(), 0)

    @override_settings(**PUBLIC)
    def test_saving_a_public_host_works(self):
        self.assertEqual(self.add("93.184.216.34").status_code, 302)
        self.assertEqual(ServerConnection.objects.count(), 1)

    @override_settings(SERVER_TESTS_PER_10_MINUTES=2)
    def test_connection_tests_are_limited_per_person(self):
        server = ServerConnection.objects.create(name="S", host="h.invalid", username="u")
        OrganizationServer.objects.create(server=server, organization=self.org)
        with mock.patch("explorer.pg.test_connection", return_value="18"):
            for _ in range(2):
                self.client.post(reverse("server_test", args=[server.pk]))
            third = self.client.post(reverse("server_test", args=[server.pk]), follow=True)
        self.assertContains(third, "Too many connection tests")


class ChecksTests(TestCase):
    @override_settings(MODE="shared", SERVER_NETWORKS="any")
    def test_a_shared_installation_that_reaches_private_networks_is_warned_about(self):
        self.assertEqual(len([w for w in run_checks() if w.id == "access_control.W005"]), 1)

    @override_settings(MODE="shared", SERVER_NETWORKS="public")
    def test_no_warning_when_public_only(self):
        self.assertEqual([w for w in run_checks() if w.id == "access_control.W005"], [])


class ThrottleTests(TestCase):
    def setUp(self):
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Org")
        self.user = get_user_model().objects.create_user("a", email="a@org.test", password="Right-pass-4471!")
        Membership.objects.create(user=self.user, organization=self.org, role="super_user")

    def attempt(self, email="a@org.test", password="wrong", address="198.51.100.1"):
        return Client(REMOTE_ADDR=address).post(reverse("login"), {"username": email, "password": password})

    def test_five_failures_lock_the_address_even_for_the_right_password(self):
        for _ in range(5):
            self.assertContains(self.attempt(), "Enter a correct email address and password.")
        locked = self.attempt(password="Right-pass-4471!")
        self.assertContains(locked, "Too many failed sign-in attempts")
        self.assertNotIn("_auth_user_id", locked.wsgi_request.session)
        self.assertEqual(AuditEntry.objects.filter(action="access.signin_locked").count(), 1)

    def test_an_email_that_does_not_exist_is_locked_with_the_same_message(self):
        for _ in range(5):
            self.attempt(email="ghost@nowhere.test")
        self.assertContains(self.attempt(email="ghost@nowhere.test"), "Too many failed sign-in attempts")

    def test_a_good_sign_in_clears_the_count(self):
        for _ in range(4):
            self.attempt()
        self.assertEqual(self.attempt(password="Right-pass-4471!").status_code, 302)
        self.assertEqual(SignInFailure.objects.filter(email="a@org.test").count(), 0)

    def test_failures_age_out(self):
        for _ in range(5):
            self.attempt()
        SignInFailure.objects.update(at=timezone.now() - timedelta(minutes=16))
        self.assertEqual(self.attempt(password="Right-pass-4471!").status_code, 302)

    @override_settings(SIGNIN_MAX_FAILURES_PER_ADDRESS=3)
    def test_one_network_address_trying_many_emails_is_locked(self):
        for number in range(3):
            self.attempt(email=f"x{number}@nowhere.test")
        self.assertContains(self.attempt(email="y@nowhere.test"), "Too many failed sign-in attempts")
        self.assertEqual(self.attempt(email="y@nowhere.test", address="198.51.100.99").status_code, 200)       # another address is not locked
        self.assertNotContains(self.attempt(email="y@nowhere.test", address="198.51.100.99"), "Too many failed")

    def test_the_command_unlocks_one_email_or_everything(self):
        for _ in range(5):
            self.attempt()
        call_command("unlock_signin", "a@org.test", stdout=StringIO())
        self.assertEqual(SignInFailure.objects.count(), 0)
        self.attempt()
        call_command("unlock_signin", "--all", stdout=StringIO())
        self.assertEqual(SignInFailure.objects.count(), 0)