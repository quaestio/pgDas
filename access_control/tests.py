from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from audit.models import AuditEntry
from servers.models import ServerConnection
from .models import DatabaseGrant, Membership, Organization, OrganizationServer
from .policy import DATABASE_ACTIONS, default_grants, filter_databases, permits, require_all, visible_servers
from .services import assign_database, revoke_database


class PermissionPolicyTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Team A")
        self.other_org = Organization.objects.create(name="Team B")
        self.admin, self.admin_member = self.employee("admin", "super_user")
        self.editor, self.editor_member = self.employee("editor", "editor")
        self.viewer, self.viewer_member = self.employee("viewer", "viewer")
        self.server = ServerConnection.objects.create(name="Local", host="localhost", username="postgres")
        self.other_server = ServerConnection.objects.create(name="Other", host="localhost", username="postgres")
        OrganizationServer.objects.create(server=self.server, organization=self.org)
        OrganizationServer.objects.create(server=self.other_server, organization=self.other_org)

    def employee(self, name, role):
        user = get_user_model().objects.create_user(name)
        member = Membership.objects.create(user=user, organization=self.org, role=role)
        return user, member

    def allowed(self, user, action, database="accounts", server=None):
        return permits(user, action, server=server or self.server, database=database)

    def test_deny_anonymous_missing_membership_and_unknown_action(self):
        self.assertFalse(self.allowed(AnonymousUser(), "read"))
        user = get_user_model().objects.create_user("legacy", is_staff=True, is_superuser=True)
        self.assertFalse(self.allowed(user, "read"))
        self.assertFalse(self.allowed(self.admin, "unknown"))

    def test_super_user_is_scoped_and_still_needs_database_identity(self):
        for action in DATABASE_ACTIONS:
            self.assertTrue(self.allowed(self.admin, action))
            self.assertFalse(self.allowed(self.admin, action, server=self.other_server))
        self.assertFalse(self.allowed(self.admin, "read", database=""))
        self.assertFalse(permits(self.admin, "servers_manage"))
        self.assertTrue(permits(self.admin, "servers_manage", organization=self.org))
        self.assertFalse(permits(self.admin, "servers_manage", organization=self.other_org))

    def test_editor_defaults_only_apply_to_assigned_database(self):
        self.assertFalse(self.allowed(self.editor, "read"))
        assign_database(self.admin, self.editor_member, self.server, "accounts")
        for action in DATABASE_ACTIONS:
            self.assertEqual(self.allowed(self.editor, action), action in {"read", "insert", "update"})
        self.assertFalse(self.allowed(self.editor, "read", database="payroll"))
        self.assertFalse(self.allowed(self.editor, "server_settings"))

    def test_separate_dangerous_action_grants(self):
        values = default_grants("editor")
        values.update(delete=True, export=True, backup_download=True)
        assign_database(self.admin, self.editor_member, self.server, "accounts", values)
        self.assertTrue(self.allowed(self.editor, "delete"))
        self.assertTrue(self.allowed(self.editor, "export"))
        self.assertTrue(self.allowed(self.editor, "backup_download"))
        self.assertFalse(self.allowed(self.editor, "truncate"))
        self.assertFalse(self.allowed(self.editor, "restore"))

    def test_the_level_is_per_database_so_one_person_can_view_one_and_edit_another(self):
        from access_control.policy import preset_permissions
        assign_database(self.admin, self.viewer_member, self.server, "accounts", preset_permissions("viewer"))
        assign_database(self.admin, self.viewer_member, self.server, "billing", preset_permissions("editor"))
        self.assertTrue(self.allowed(self.viewer, "read"))
        self.assertFalse(self.allowed(self.viewer, "insert"))
        self.assertTrue(permits(self.viewer, "insert", server=self.server, database="billing"))
        self.assertFalse(permits(self.viewer, "drop", server=self.server, database="billing"))
        self.assertFalse(permits(self.viewer, "read", server=self.server, database="other"))

    def test_levels_are_recognised_from_the_flags(self):
        from access_control.policy import level_of, preset_permissions
        for level in ("viewer", "editor", "designer"):
            grant = assign_database(self.admin, self.viewer_member, self.server, "lv", preset_permissions(level))
            self.assertEqual(level_of(grant), level)
        grant = assign_database(self.admin, self.viewer_member, self.server, "lv", {"read": True, "drop": True})
        self.assertEqual(level_of(grant), "custom")

    def test_the_refusal_says_which_access_the_person_has_and_what_to_ask_for(self):
        from access_control.policy import preset_permissions, require
        assign_database(self.admin, self.viewer_member, self.server, "accounts", preset_permissions("viewer"))
        with self.assertRaisesMessage(PermissionDenied, "your access to 'accounts' on") as raised:
            require(self.viewer, "insert", server=self.server, database="accounts")
        self.assertIn("is Viewer", str(raised.exception))
        self.assertIn("PGDAS blocked this", str(raised.exception))
        with self.assertRaisesMessage(PermissionDenied, "you have no access to"):
            require(self.viewer, "read", server=self.server, database="secret")
        with self.assertRaisesMessage(PermissionDenied, "reserved for Super Users"):
            require(self.viewer, "servers_manage", organization=self.org)

    def test_deactivation_and_revocation_apply_to_existing_user_object(self):
        assign_database(self.admin, self.editor_member, self.server, "accounts")
        get_user_model().objects.filter(pk=self.editor.pk).update(is_active=False)
        self.assertFalse(self.allowed(self.editor, "read"))
        get_user_model().objects.filter(pk=self.editor.pk).update(is_active=True)
        self.assertTrue(self.allowed(self.editor, "read"))
        revoke_database(self.admin, self.editor_member, self.server, "accounts")
        self.assertFalse(self.allowed(self.editor, "read"))

    def test_disabled_organization_and_role_demotion_take_effect_immediately(self):
        Organization.objects.filter(pk=self.org.pk).update(active=False)
        self.assertFalse(self.allowed(self.admin, "read"))
        Organization.objects.filter(pk=self.org.pk).update(active=True)
        Membership.objects.filter(pk=self.admin_member.pk).update(role="viewer")
        self.assertFalse(self.allowed(self.admin, "read"))

    def test_database_names_are_exact_including_case_spaces_and_symbols(self):
        assign_database(self.admin, self.viewer_member, self.server, "Sales-DB বাংলা")
        self.assertTrue(self.allowed(self.viewer, "read", "Sales-DB বাংলা"))
        self.assertFalse(self.allowed(self.viewer, "read", "sales-db বাংলা"))

    def test_cross_organization_grant_cannot_authorize_access(self):
        DatabaseGrant.objects.create(membership=self.viewer_member, server=self.other_server, database="accounts")
        self.assertFalse(self.allowed(self.viewer, "read", server=self.other_server))
        self.assertFalse(visible_servers(self.viewer).exists())

    def test_no_read_means_no_database_actions(self):
        DatabaseGrant.objects.create(membership=self.editor_member, server=self.server, database="accounts", read=False, insert=True)
        self.assertFalse(self.allowed(self.editor, "insert"))
        self.assertFalse(visible_servers(self.editor).exists())

    def test_mixed_actions_all_required_and_empty_list_denied(self):
        assign_database(self.admin, self.editor_member, self.server, "accounts")
        require_all(self.editor, ["read", "update"], server=self.server, database="accounts")
        for actions in ([], ["read", "delete"], ["read", "unknown"]):
            with self.assertRaises(PermissionDenied):
                require_all(self.editor, actions, server=self.server, database="accounts")

    def test_visible_names_are_intersection_with_postgres_results(self):
        assign_database(self.admin, self.viewer_member, self.server, "accounts")
        self.assertEqual(list(visible_servers(self.viewer)), [self.server])
        self.assertEqual(filter_databases(self.viewer, self.server, [{"name": "accounts"}, {"name": "payroll"}]), [{"name": "accounts"}])
        self.assertEqual(filter_databases(self.viewer, self.server, []), [])

    def test_grant_service_requires_super_user_and_valid_permissions(self):
        with self.assertRaises(PermissionDenied):
            assign_database(self.editor, self.viewer_member, self.server, "accounts")
        with self.assertRaises(PermissionDenied):
            assign_database(self.admin, self.viewer_member, self.other_server, "accounts")
        for permissions in ({"unknown": True}, {"read": "false"}):
            with self.assertRaises(ValidationError):
                assign_database(self.admin, self.viewer_member, self.server, "accounts", permissions)
        self.assertFalse(DatabaseGrant.objects.exists())

    def test_assignment_audited_and_rolled_back_if_audit_fails(self):
        with patch("access_control.services.record", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                assign_database(self.admin, self.editor_member, self.server, "accounts")
        self.assertFalse(DatabaseGrant.objects.exists())
        assign_database(self.admin, self.editor_member, self.server, "accounts")
        self.assertTrue(AuditEntry.objects.filter(action="permission.assign", user=self.admin).exists())
        revoke_database(self.admin, self.editor_member, self.server, "accounts")
        self.assertTrue(AuditEntry.objects.filter(action="permission.revoke", user=self.admin).exists())

    def test_invalid_database_names_are_not_saved(self):
        for name in ("", "bad\x00name", "আ" * 22):
            with self.assertRaises(ValidationError):
                assign_database(self.admin, self.viewer_member, self.server, name)
        self.assertFalse(DatabaseGrant.objects.exists())
