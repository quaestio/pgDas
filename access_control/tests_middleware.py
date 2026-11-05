"""The request gate (AccessMiddleware): setup first, then who may open which page, and what a refusal looks like."""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from audit.models import AuditEntry
from servers.models import ServerConnection

from .models import Installation, Membership, Organization, OrganizationServer


class GateTests(TestCase):
    def setUp(self):
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Team A")
        self.other_org = Organization.objects.create(name="Team B")
        User = get_user_model()
        self.admin = User.objects.create_user("admin", password="pw-12345-abc")
        Membership.objects.create(user=self.admin, organization=self.org, role="super_user")
        self.viewer = User.objects.create_user("viewer", password="pw-12345-abc")
        Membership.objects.create(user=self.viewer, organization=self.org, role="viewer")
        self.server = ServerConnection.objects.create(name="Mine", host="localhost", username="u")
        OrganizationServer.objects.create(server=self.server, organization=self.org)
        self.foreign = ServerConnection.objects.create(name="Theirs", host="localhost", username="u")
        OrganizationServer.objects.create(server=self.foreign, organization=self.other_org)

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_nothing_opens_before_the_installation_is_complete(self):
        Installation.objects.filter(pk=1).update(completed=False)
        for url in ("/", "/login/", "/audit/"):
            self.assertRedirects(Client().get(url), reverse("setup"), fetch_redirect_response=False)

    def test_a_signed_in_person_with_no_membership_is_refused(self):
        stray = get_user_model().objects.create_user("stray", password="pw-12345-abc")
        self.assertEqual(self.client_for(stray).get("/").status_code, 403)

    def test_a_refusal_is_recorded_whatever_the_page(self):
        response = self.client_for(self.viewer).get(reverse("monitor"))
        self.assertEqual(response.status_code, 403)
        entry = AuditEntry.objects.get(action="access.denied")
        self.assertEqual((entry.ok, entry.username, entry.detail), (False, "viewer", "/monitor/"))

    def test_the_page_scripts_get_json_refusals_not_html(self):
        client = self.client_for(self.viewer)
        for url in (reverse("monitor_data"), reverse("watch_data")):
            response = client.get(url, {"server": self.server.pk})
            self.assertEqual(response.status_code, 403, url)
            self.assertEqual(response["Content-Type"], "application/json")
            self.assertFalse(response.json()["ok"])
        fetched = client.post(reverse("monitor_stop"), {"server": self.server.pk, "pid": 1}, headers={"X-Requested-With": "fetch"})
        self.assertEqual((fetched.status_code, fetched["Content-Type"]), (403, "application/json"))
        self.assertEqual(AuditEntry.objects.filter(action="access.denied").count(), 3)

    def test_a_server_of_another_organization_does_not_exist_for_you(self):
        client = self.client_for(self.admin)
        self.assertEqual(client.get(reverse("tree"), {"type": "server", "server": self.foreign.pk}).status_code, 404)
        self.assertEqual(client.get(reverse("detail"), {"type": "server", "server": self.foreign.pk}).status_code, 404)
        self.assertEqual(client.get(reverse("monitor_data"), {"server": self.foreign.pk}).status_code, 404)

    def test_a_server_value_that_is_not_a_number_is_not_found_not_a_crash(self):
        client = self.client_for(self.admin)
        for value in ("abc", "1; DROP TABLE x", "-1", "1.5", "%00"):
            for url in (reverse("tree"), reverse("detail"), reverse("monitor_data")):
                self.assertEqual(client.get(url, {"server": value}).status_code, 404, (url, value))
        self.assertEqual(client.post(reverse("backup_create"), {"server": "abc", "database": "x"}).status_code, 404)

    def test_pages_for_everyone_who_belongs_stay_open_to_a_viewer(self):
        client = self.client_for(self.viewer)
        for url in ("/", "/audit/", "/backups/"):
            self.assertEqual(client.get(url).status_code, 200, url)
