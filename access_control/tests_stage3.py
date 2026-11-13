"""Stage 3: the editing screens load for people who may edit; server-wide access follows new databases."""
from contextlib import contextmanager
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from io import StringIO

from servers.models import ServerConnection

from . import serverwide
from .models import DatabaseGrant, Installation, Membership, Organization, OrganizationServer, ServerWideAccess
from .policy import level_of, preset_permissions
from .services import assign_database

DATABASES = ["sales", "hr"]


def fake_connect(server, database=None):
    @contextmanager
    def manager():
        yield list(DATABASES)
    return manager()


def fake_list(conn):
    return [{"name": name} for name in conn]


class Fixture(TestCase):
    def setUp(self):
        del DATABASES[:]
        DATABASES.extend(["sales", "hr"])
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Org")
        self.admin = get_user_model().objects.create_user("a", email="a@o.test", password="pw-12345-abc", is_staff=True, is_superuser=True, email_verified=True)
        Membership.objects.create(user=self.admin, organization=self.org, role="super_user")
        self.server = ServerConnection.objects.create(name="S", host="h", username="u")
        OrganizationServer.objects.create(server=self.server, organization=self.org)
        self.person = get_user_model().objects.create_user("p", email="p@o.test", password="pw-12345-abc", email_verified=True)
        self.member = Membership.objects.create(user=self.person, organization=self.org, role="viewer")
        self.patches = [patch("explorer.pg.connect", fake_connect), patch("explorer.pg.list_databases", fake_list)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def admin_client(self):
        client = Client()
        client.force_login(self.admin)
        return client


class EditingScreensTests(Fixture):
    def page(self, user):
        client = Client()
        client.force_login(user)
        return client.get(reverse("employee_list") if user == self.admin else reverse("server_list")).content.decode()

    def test_the_editing_scripts_load_for_super_users_and_for_people_with_a_write_permission_only(self):
        self.assertIn("row_edit.js", self.page(self.admin))
        assign_database(self.admin, self.member, self.server, "sales", preset_permissions("viewer"))
        self.assertNotIn("row_edit.js", self.page(self.person))                       # a viewer gets the read-only screens
        assign_database(self.admin, self.member, self.server, "hr", preset_permissions("editor"))
        page = self.page(self.person)
        self.assertIn("row_edit.js", page)
        self.assertIn("table_dialogs.js", page)

    def test_a_maintenance_permission_alone_also_loads_them(self):
        assign_database(self.admin, self.member, self.server, "sales", {**preset_permissions("viewer"), "maintenance": True})
        self.assertIn("row_edit.js", self.page(self.person))


class ServerWideTests(Fixture):
    def follow(self, **extra):
        return self.admin_client().post(reverse("employee_access", args=[self.person.pk]), {f"all-{self.server.pk}": "editor", f"follow-{self.server.pk}": "on", **extra})

    def test_following_gives_every_database_and_remembers_it(self):
        self.assertEqual(self.follow().status_code, 302)
        self.assertEqual(sorted(DatabaseGrant.objects.filter(membership=self.member).values_list("database", flat=True)), ["hr", "sales"])
        entry = ServerWideAccess.objects.get(membership=self.member, server=self.server)
        self.assertEqual((entry.level, entry.excluded), ("editor", []))

    def test_a_database_created_later_is_picked_up_by_the_command_and_at_sign_in(self):
        self.follow()
        DATABASES.append("logs")
        call_command("sync_access", stdout=StringIO())
        grant = DatabaseGrant.objects.get(membership=self.member, database="logs")
        self.assertEqual(level_of(grant), "editor")
        DATABASES.append("archive")
        Client().post(reverse("login"), {"username": "p@o.test", "password": "pw-12345-abc"})
        self.assertTrue(DatabaseGrant.objects.filter(membership=self.member, database="archive").exists())

    def test_a_database_set_apart_stays_out(self):
        self.follow()
        index = {r["name"]: r["index"] for r in self.admin_client().get(reverse("employee_access", args=[self.person.pk])).context["sections"][0]["rows"]}
        self.admin_client().post(reverse("employee_access", args=[self.person.pk]),
                                 {f"follow-{self.server.pk}": "on", f"lvl-{self.server.pk}-{index['sales']}": "editor", f"lvl-{self.server.pk}-{index['hr']}": "none"})
        self.assertFalse(DatabaseGrant.objects.filter(membership=self.member, database="hr").exists())
        DATABASES.append("logs")
        serverwide.sync_all()
        self.assertFalse(DatabaseGrant.objects.filter(membership=self.member, database="hr").exists())
        self.assertTrue(DatabaseGrant.objects.filter(membership=self.member, database="logs").exists())
        self.assertEqual(ServerWideAccess.objects.get(membership=self.member).excluded, ["hr"])

    def test_unticking_stops_following_but_keeps_what_was_given(self):
        self.follow()
        self.admin_client().post(reverse("employee_access", args=[self.person.pk]), {})
        self.assertFalse(ServerWideAccess.objects.exists())
        DATABASES.append("logs")
        serverwide.sync_all()
        self.assertFalse(DatabaseGrant.objects.filter(membership=self.member, database="logs").exists())

    def test_following_needs_a_level(self):
        response = self.admin_client().post(reverse("employee_access", args=[self.person.pk]), {f"follow-{self.server.pk}": "on"}, follow=True)
        self.assertContains(response, "Choose the level for every database")
        self.assertFalse(ServerWideAccess.objects.exists())

    def test_an_unreachable_server_never_breaks_sign_in(self):
        self.follow()
        with patch("access_control.serverwide.database_names", side_effect=RuntimeError("down")):
            response = Client().post(reverse("login"), {"username": "p@o.test", "password": "pw-12345-abc"})
        self.assertEqual(response.status_code, 302)