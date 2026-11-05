"""Allowed addresses: the list parser, the client address (and who may be believed about it), and the check on every request."""
import ipaddress

from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from audit.models import AuditEntry
from servers.models import ServerConnection

from . import network
from .models import Installation, Membership, Organization, OrganizationServer


class ParserTests(SimpleTestCase):
    def test_addresses_ranges_comments_commas_and_both_families(self):
        networks, bad = network.parse_networks("10.0.0.5\n 192.168.1.0/24, 2001:db8::/32  # office\n\n# nothing\n203.0.113.9/32;::1")
        self.assertEqual([str(n) for n in networks], ["10.0.0.5/32", "192.168.1.0/24", "2001:db8::/32", "203.0.113.9/32", "::1/128"])
        self.assertEqual(bad, [])

    def test_bad_entries_are_reported_by_name(self):
        _, bad = network.parse_networks("10.0.0.5\nnot-an-ip\n300.1.1.1\n10.0.0.0/33\nlocalhost")
        self.assertEqual(bad, ["not-an-ip", "300.1.1.1", "10.0.0.0/33", "localhost"])

    def test_normalise_writes_one_clean_entry_per_line(self):
        self.assertEqual(network.normalise("10.0.0.9/24, 203.0.113.7/32\n::1"), "10.0.0.0/24\n203.0.113.7\n::1")
        self.assertEqual(network.normalise("   \n# only a comment"), "")

    def test_an_empty_list_allows_everyone_a_filled_one_only_its_addresses(self):
        here = ipaddress.ip_address("10.0.0.5")
        self.assertTrue(network.allowed("", here))
        self.assertTrue(network.allowed("", None))
        self.assertTrue(network.allowed("10.0.0.0/24", here))
        self.assertFalse(network.allowed("10.0.1.0/24", here))
        self.assertFalse(network.allowed("10.0.0.0/24", None))                               # an unknown address is never inside a list
        self.assertFalse(network.allowed("2001:db8::/32", here))                             # a different family never matches
        self.assertTrue(network.allowed("2001:db8::/32", ipaddress.ip_address("2001:db8::1")))
        self.assertTrue(network.allowed("203.0.113.9", ipaddress.ip_address("::ffff:203.0.113.9")))   # an IPv4 address seen as IPv6


class ClientAddressTests(SimpleTestCase):
    def request(self, remote="203.0.113.9", forwarded=None):
        extra = {"REMOTE_ADDR": remote}
        if forwarded is not None:
            extra["HTTP_X_FORWARDED_FOR"] = forwarded
        return RequestFactory().get("/", **extra)

    def test_the_connecting_address_is_used_and_a_forwarded_header_is_ignored_without_trusted_proxies(self):
        self.assertEqual(str(network.client_ip(self.request(forwarded="10.0.0.5"))), "203.0.113.9")

    @override_settings(TRUSTED_PROXIES=["127.0.0.1", "10.0.0.0/8"])
    def test_a_trusted_proxy_may_say_who_the_visitor_is_taking_the_first_untrusted_address_from_the_right(self):
        self.assertEqual(str(network.client_ip(self.request("127.0.0.1", "198.51.100.4"))), "198.51.100.4")
        # a forged first entry does not help: the client address is the right-most one the proxies did not add themselves
        self.assertEqual(str(network.client_ip(self.request("127.0.0.1", "10.0.0.5, 198.51.100.4, 10.1.1.1"))), "198.51.100.4")
        self.assertEqual(str(network.client_ip(self.request("127.0.0.1", "10.2.2.2, 10.1.1.1"))), "127.0.0.1")     # nobody untrusted in the chain

    @override_settings(TRUSTED_PROXIES=["127.0.0.1"])
    def test_a_header_from_anyone_else_and_a_malformed_header_are_not_believed(self):
        self.assertEqual(str(network.client_ip(self.request("203.0.113.9", "10.0.0.5"))), "203.0.113.9")
        self.assertIsNone(network.client_ip(self.request("127.0.0.1", "198.51.100.4, garbage")))            # what the proxy appended is unreadable
        self.assertEqual(str(network.client_ip(self.request("127.0.0.1", "garbage, 198.51.100.4"))), "198.51.100.4")


class EveryRequestTests(TestCase):
    def setUp(self):
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Team A", allowed_ips="10.0.0.0/24\n2001:db8::/32")
        User = get_user_model()
        self.admin = User.objects.create_user("a", email="admin@example.test", password="pw-12345-abc")
        Membership.objects.create(user=self.admin, organization=self.org, role="super_user")
        self.editor = User.objects.create_user("e", email="editor@example.test", password="pw-12345-abc")
        Membership.objects.create(user=self.editor, organization=self.org, role="editor")
        self.viewer = User.objects.create_user("v", email="viewer@example.test", password="pw-12345-abc")
        Membership.objects.create(user=self.viewer, organization=self.org, role="viewer")
        self.server = ServerConnection.objects.create(name="S", host="localhost", username="u")
        OrganizationServer.objects.create(server=self.server, organization=self.org)

    def as_user(self, user, remote):
        client = Client(REMOTE_ADDR=remote)
        client.force_login(user)
        return client

    def test_inside_the_list_everyone_works_outside_it_only_super_users(self):
        for user in (self.admin, self.editor, self.viewer):
            self.assertEqual(self.as_user(user, "10.0.0.77").get("/").status_code, 200, user.email)
            self.assertEqual(self.as_user(user, "2001:db8::5").get("/").status_code, 200, user.email)
        self.assertEqual(self.as_user(self.admin, "198.51.100.1").get("/").status_code, 200)       # a Super User from anywhere
        for user in (self.editor, self.viewer):
            response = self.as_user(user, "198.51.100.1").get("/")
            self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)

    def test_an_empty_list_restricts_nobody(self):
        Organization.objects.filter(pk=self.org.pk).update(allowed_ips="")
        self.assertEqual(self.as_user(self.viewer, "198.51.100.1").get("/").status_code, 200)
        Organization.objects.filter(pk=self.org.pk).update(allowed_ips="   \n")
        self.assertEqual(self.as_user(self.viewer, "198.51.100.1").get("/").status_code, 200)

    def test_a_refusal_ends_the_session_says_why_and_is_recorded_with_the_address(self):
        client = self.as_user(self.viewer, "198.51.100.1")
        client.get("/")
        page = client.get(reverse("login"))
        self.assertContains(page, "Access from this address is not allowed")
        self.assertNotIn("_auth_user_id", client.session)                                           # signed out
        entry = AuditEntry.objects.get(action="access.address_refused")
        self.assertEqual((entry.ok, entry.username), (False, "viewer@example.test"))
        self.assertIn("198.51.100.1", entry.detail)
        self.assertNotIn("10.0.0.0/24", page.content.decode())                                      # the list itself is never shown

    def test_changing_the_list_takes_effect_on_the_next_request(self):
        client = self.as_user(self.viewer, "198.51.100.1")
        Organization.objects.filter(pk=self.org.pk).update(allowed_ips="")
        self.assertEqual(client.get("/").status_code, 200)
        Organization.objects.filter(pk=self.org.pk).update(allowed_ips="203.0.113.0/24")
        self.assertEqual(client.get("/").status_code, 302)

    def test_page_scripts_get_a_json_answer_when_they_are_refused(self):
        response = self.as_user(self.editor, "198.51.100.1").get(reverse("tree"), {"type": "servers"})
        self.assertEqual((response.status_code, response["Content-Type"]), (403, "application/json"))
        self.assertTrue(response.json()["signed_out"])

    @override_settings(TRUSTED_PROXIES=[])
    def test_a_forged_forwarded_header_does_not_open_the_door(self):
        client = Client(REMOTE_ADDR="198.51.100.1", HTTP_X_FORWARDED_FOR="10.0.0.5")
        client.force_login(self.viewer)
        self.assertEqual(client.get("/").status_code, 302)

    @override_settings(TRUSTED_PROXIES=["127.0.0.1"])
    def test_behind_a_trusted_proxy_the_forwarded_address_counts_and_forging_it_does_not_help(self):
        honest = Client(REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="10.0.0.9")
        honest.force_login(self.viewer)
        self.assertEqual(honest.get("/").status_code, 200)
        forger = Client(REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="10.0.0.9, 198.51.100.1")      # the proxy appended the real address
        forger.force_login(self.viewer)
        self.assertEqual(forger.get("/").status_code, 302)

    def test_login_and_logout_pages_are_not_blocked_and_other_organizations_are_not_affected(self):
        self.assertEqual(Client(REMOTE_ADDR="198.51.100.1").get(reverse("login")).status_code, 200)
        other = Organization.objects.create(name="Team B")
        person = get_user_model().objects.create_user("o", email="other@example.test", password="pw-12345-abc")
        Membership.objects.create(user=person, organization=other, role="viewer")
        self.assertEqual(self.as_user(person, "198.51.100.1").get("/").status_code, 200)               # no list in their organization
