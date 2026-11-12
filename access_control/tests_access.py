"""The access matrix: per employee, per server, per database, a level (None / Viewer / Editor / Designer)."""
from unittest.mock import patch
from contextlib import contextmanager

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from audit.models import AuditEntry
from servers.models import ServerConnection

from .models import DatabaseGrant, Installation, Membership, Organization, OrganizationServer
from .policy import level_of, permits

DATABASES = {"Alpha": ["sales", "hr", "public_data"], "Beta": ["logs"]}


def fake_connect(server, database=None):
    @contextmanager
    def manager():
        yield DATABASES[server.name]
    return manager()


def fake_list(conn):
    return [{"name": name} for name in conn]


class AccessMatrixTests(TestCase):
    def setUp(self):
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Org")
        user = get_user_model().objects.create_user("a", email="a@o.test", password="pw-12345-abc", is_staff=True, is_superuser=True)
        self.admin_member = Membership.objects.create(user=user, organization=self.org, role="super_user")
        self.alpha = ServerConnection.objects.create(name="Alpha", host="h1", username="u")
        self.beta = ServerConnection.objects.create(name="Beta", host="h2", username="u")
        for server in (self.alpha, self.beta):
            OrganizationServer.objects.create(server=server, organization=self.org)
        self.person = get_user_model().objects.create_user("p", email="p@o.test", password="pw-12345-abc")
        self.member = Membership.objects.create(user=self.person, organization=self.org, role="viewer")
        self.client = Client()
        self.client.force_login(user)
        self.patches = [patch("explorer.pg.connect", fake_connect), patch("explorer.pg.list_databases", fake_list)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def url(self):
        return reverse("employee_access", args=[self.person.pk])

    def save(self, **levels):
        data = {}
        for key, value in levels.items():
            data[key.replace("_", "-", 1)] = value
        return self.client.post(self.url(), data)

    def row(self, server, name):
        section = next(s for s in self.client.get(self.url()).context["sections"] if s["server"] == server)
        return next(r for r in section["rows"] if r["name"] == name)

    def test_every_server_and_database_is_listed_with_level_none(self):
        page = self.client.get(self.url())
        self.assertEqual([s["server"].name for s in page.context["sections"]], ["Alpha", "Beta"])
        for needle in ("sales", "hr", "public_data", "logs"):
            self.assertContains(page, needle)
        self.assertEqual(self.row(self.alpha, "sales")["level"], "none")

    def test_one_person_can_be_viewer_on_one_database_and_editor_on_another(self):
        index = {r["name"]: r["index"] for r in self.client.get(self.url()).context["sections"][0]["rows"]}
        self.assertEqual(self.client.post(self.url(), {f"lvl-{self.alpha.pk}-{index['sales']}": "viewer", f"lvl-{self.alpha.pk}-{index['hr']}": "editor"}).status_code, 302)
        self.assertTrue(permits(self.person, "read", server=self.alpha, database="sales"))
        self.assertFalse(permits(self.person, "insert", server=self.alpha, database="sales"))
        self.assertTrue(permits(self.person, "insert", server=self.alpha, database="hr"))
        self.assertFalse(permits(self.person, "read", server=self.alpha, database="public_data"))
        self.assertEqual(self.row(self.alpha, "sales")["level"], "viewer")
        self.assertEqual(self.row(self.alpha, "hr")["level"], "editor")

    def test_a_whole_server_can_be_set_at_once_and_another_server_is_untouched(self):
        self.client.post(self.url(), {f"all-{self.alpha.pk}": "designer"})
        self.assertEqual(DatabaseGrant.objects.filter(membership=self.member, server=self.alpha).count(), 3)
        self.assertTrue(all(level_of(g) == "designer" for g in DatabaseGrant.objects.filter(membership=self.member)))
        self.assertFalse(DatabaseGrant.objects.filter(membership=self.member, server=self.beta).exists())

    def test_none_removes_access_and_a_custom_grant_is_left_alone(self):
        DatabaseGrant.objects.create(membership=self.member, server=self.alpha, database="sales", read=True, drop=True)       # custom
        DatabaseGrant.objects.create(membership=self.member, server=self.alpha, database="hr", read=True)
        index = {r["name"]: r["index"] for r in self.client.get(self.url()).context["sections"][0]["rows"]}
        self.assertEqual(self.row(self.alpha, "sales")["level"], "custom")
        self.client.post(self.url(), {f"lvl-{self.alpha.pk}-{index['sales']}": "custom", f"lvl-{self.alpha.pk}-{index['hr']}": "none"})
        self.assertTrue(DatabaseGrant.objects.filter(membership=self.member, database="sales", drop=True).exists())
        self.assertFalse(DatabaseGrant.objects.filter(membership=self.member, database="hr").exists())

    def test_changes_are_audited(self):
        self.client.post(self.url(), {f"all-{self.beta.pk}": "viewer"})
        self.assertTrue(AuditEntry.objects.filter(action="permission.assign").exists())

    def test_a_server_that_cannot_be_reached_says_so_and_does_not_break_the_page(self):
        from explorer import pg
        def broken(server, database=None):
            raise pg.PgError("connection refused")
        with patch("explorer.pg.connect", broken):
            page = self.client.get(self.url())
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Could not list the databases: connection refused")

    def test_only_super_users_can_open_it_and_a_super_user_employee_needs_no_levels(self):
        other = Client()
        other.force_login(self.person)
        self.assertEqual(other.get(self.url()).status_code, 403)
        self.assertEqual(other.post(self.url(), {f"all-{self.alpha.pk}": "designer"}).status_code, 403)
        self.assertFalse(DatabaseGrant.objects.exists())
        self.assertContains(self.client.get(reverse("employee_access", args=[self.admin_member.user_id])), "Super Users reach every server")

    def test_the_employee_list_links_to_it(self):
        self.assertContains(self.client.get(reverse("employee_list")), reverse("employee_access", args=[self.person.pk]))

    def test_run_sql_is_a_checkbox_per_database_and_keeps_the_level_and_the_restricted_login_extras(self):
        index = {r["name"]: r["index"] for r in self.client.get(self.url()).context["sections"][0]["rows"]}
        key = f"{self.alpha.pk}-{index['sales']}"
        self.client.post(self.url(), {f"lvl-{key}": "editor", f"sql-{key}": "on"})
        grant = DatabaseGrant.objects.get(membership=self.member, database="sales")
        self.assertEqual((level_of(grant), grant.run_sql), ("editor", True))
        DatabaseGrant.objects.filter(pk=grant.pk).update(functions=True)                     # set under Advanced
        self.client.post(self.url(), {f"lvl-{key}": "designer", f"sql-{key}": "on"})
        grant.refresh_from_db()
        self.assertEqual((level_of(grant), grant.run_sql, grant.functions), ("designer", True, True))
        self.client.post(self.url(), {f"lvl-{key}": "designer"})                              # box cleared
        grant.refresh_from_db()
        self.assertEqual((level_of(grant), grant.run_sql), ("designer", False))
        self.assertContains(self.client.get(self.url()), "Run SQL")
