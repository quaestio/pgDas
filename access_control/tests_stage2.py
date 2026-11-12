"""Stage 2 permissions: Run SQL, views, sequences, schemas, and functions / triggers / extensions (restricted login only); and the UI following the grants."""
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import TestCase

from servers.models import ServerConnection

from .models import DatabaseGrant, Installation, Membership, Organization, OrganizationServer
from .policy import permits, preset_permissions, refusal_message
from .services import assign_database
from .sql_policy import authorize


class ObjectPermissionTests(TestCase):
    def setUp(self):
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Org")
        admin = get_user_model().objects.create_user("a", email="a@o.test", password="x", is_staff=True, is_superuser=True)
        Membership.objects.create(user=admin, organization=self.org, role="super_user")
        self.admin = admin
        self.server = ServerConnection.objects.create(name="S", host="h", username="u")
        OrganizationServer.objects.create(server=self.server, organization=self.org)
        self.person = get_user_model().objects.create_user("p", email="p@o.test", password="x")
        self.member = Membership.objects.create(user=self.person, organization=self.org, role="editor")

    def grant(self, level="designer", **extra):
        values = preset_permissions(level)
        values.update(extra)
        return assign_database(self.admin, self.member, self.server, "db", values)

    def write(self, sql):
        return authorize(self.person, self.server, "db", sql, write=True)

    def test_run_sql_is_a_permission_of_its_own_and_off_by_default(self):
        self.grant("viewer")
        with self.assertRaisesMessage(PermissionDenied, "run your own SQL"):
            authorize(self.person, self.server, "db", "SELECT 1")
        self.grant("viewer", run_sql=True)
        authorize(self.person, self.server, "db", "SELECT 1")

    def test_views_sequences_and_schemas_follow_their_own_permission(self):
        self.grant("editor")
        for sql in ("CREATE VIEW v AS SELECT 1", "CREATE SEQUENCE s", "CREATE SCHEMA x", "DROP VIEW v", "DROP SEQUENCE s", "DROP SCHEMA x"):
            with self.subTest(sql=sql), self.assertRaises(PermissionDenied):
                self.write(sql)
        self.grant("designer")
        for sql in ("CREATE VIEW v AS SELECT 1", "CREATE SEQUENCE s", "CREATE SCHEMA x", "DROP VIEW v", "DROP SEQUENCE s", "DROP SCHEMA x"):
            with self.subTest(sql=sql):
                self.write(sql)

    def test_a_schema_cannot_be_given_to_another_owner(self):
        self.grant("designer")
        with self.assertRaisesMessage(PermissionDenied, "AUTHORIZATION"):
            self.write("CREATE SCHEMA x AUTHORIZATION someone")

    def test_functions_triggers_and_extensions_need_the_restricted_login_even_when_ticked(self):
        self.grant("designer", functions=True, triggers=True, extensions=True)
        statements = ("CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$", "DROP FUNCTION f()",
                      "CREATE EXTENSION pg_trgm", "DROP EXTENSION pg_trgm",
                      "CREATE TRIGGER t AFTER INSERT ON public.people FOR EACH ROW EXECUTE FUNCTION f()", "DROP TRIGGER t ON public.people")
        for sql in statements:
            with self.subTest(sql=sql), self.assertRaises(PermissionDenied):
                self.write(sql)
        self.assertIn("needs a restricted PostgreSQL login", refusal_message(self.person, "functions", server=self.server, database="db"))
        DatabaseGrant.objects.filter(membership=self.member).update(execution_username="restricted_login")
        for sql in statements:
            with self.subTest(sql=sql, restricted=True):
                self.write(sql)

    def test_a_function_without_its_permission_is_refused_even_with_a_restricted_login(self):
        self.grant("designer")
        DatabaseGrant.objects.filter(membership=self.member).update(execution_username="restricted_login")
        with self.assertRaises(PermissionDenied):
            self.write("CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$")

    def test_security_definer_functions_are_refused(self):
        self.grant("designer", functions=True)
        DatabaseGrant.objects.filter(membership=self.member).update(execution_username="restricted_login")
        with self.assertRaises(PermissionDenied):
            self.write("CREATE FUNCTION f() RETURNS int LANGUAGE sql SECURITY DEFINER AS $$ SELECT 1 $$")

    def test_the_extras_are_not_part_of_the_level(self):
        from .policy import level_of
        grant = self.grant("editor", run_sql=True, functions=True)
        self.assertEqual(level_of(grant), "editor")


class InterfaceFollowsGrantsTests(ObjectPermissionTests):
    def test_the_capability_helpers_follow_the_database_level(self):
        from explorer.views import _can_rows, _can_structure
        self.grant("viewer")
        self.assertFalse(_can_rows(self.person, self.server, "db"))
        self.assertFalse(_can_structure(self.person, self.server, "db"))
        self.grant("editor")
        self.assertTrue(_can_rows(self.person, self.server, "db"))
        self.assertFalse(_can_structure(self.person, self.server, "db"))
        self.grant("designer")
        self.assertTrue(_can_structure(self.person, self.server, "db"))
        self.assertFalse(_can_rows(self.person, self.server, "other"))
        self.assertTrue(_can_rows(self.admin, self.server, None))

    def test_an_existing_grant_keeps_its_old_behaviour_through_the_migration_rules(self):
        grant = DatabaseGrant.objects.create(membership=self.member, server=self.server, database="old", read=True, create=True)
        self.assertFalse(grant.run_sql)             # new grants start without it (the migration only back-fills rows that already exist)