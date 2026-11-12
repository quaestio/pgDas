import json
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.urls import reverse
from access_control.models import Organization, OrganizationServer, Membership, DatabaseGrant, Installation
from access_control.sql_policy import authorize
from access_control.services import assign_database
from servers.models import ServerConnection


class WorkflowTests(TestCase):
    def test_employee_popup_and_delete_protection(self):
        response = self.client.get(reverse("employee_edit", args=[self.editor.pk]), HTTP_X_EMPLOYEE_MODAL="1")
        self.assertContains(response, 'id="employeeDialogTitle"')
        self.assertNotContains(response, "<html")
        response = self.client.post(reverse("employee_delete", args=[self.admin.pk]), {"confirm": "delete"}, HTTP_X_EMPLOYEE_MODAL="1")
        self.assertContains(response, "Keep at least one active Super User")
        self.admin.refresh_from_db()
        self.assertIsNone(self.admin.deleted_at)

    def test_employee_delete_revokes_access_but_keeps_audit_history(self):
        from audit.models import record, AuditEntry
        entry = record(self.editor, "sql.run", self.server, "SELECT 1", payload={"database": "allowed"})
        self.client.get(reverse("employee_delete", args=[self.editor.pk]))
        self.editor.refresh_from_db()
        self.assertTrue(self.editor.is_active)
        response = self.client.post(reverse("employee_delete", args=[self.editor.pk]), {"confirm": "delete"}, HTTP_X_EMPLOYEE_MODAL="1")
        self.assertTrue(response.json()["ok"])
        self.editor.refresh_from_db()
        self.assertFalse(self.editor.is_active)
        self.assertIsNotNone(self.editor.deleted_at)
        self.assertFalse(DatabaseGrant.objects.filter(membership=self.editor.pgdas_membership).exists())
        self.assertEqual(AuditEntry.objects.get(pk=entry.pk).user_id, self.editor.pk)
        self.assertNotContains(self.client.get(reverse("employee_list")), reverse("employee_edit", args=[self.editor.pk]))
        self.assertEqual(self.client.get(reverse("employee_edit", args=[self.editor.pk])).status_code, 404)

    def setUp(self):
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Test organization")
        self.admin = get_user_model().objects.create_user("admin", password="test-pass", is_staff=True, is_superuser=True)
        self.editor = get_user_model().objects.create_user("editor", password="test-pass", is_staff=True)
        self.viewer = get_user_model().objects.create_user("viewer", password="test-pass")
        for user, role in ((self.admin, "super_user"), (self.editor, "editor"), (self.viewer, "viewer")):
            Membership.objects.create(user=user, organization=self.org, role=role)
        self.server = ServerConnection.objects.create(name="Test", host="localhost", username="postgres")
        OrganizationServer.objects.create(server=self.server, organization=self.org)
        assign_database(self.admin, self.editor.pgdas_membership, self.server, "allowed")
        assign_database(self.admin, self.viewer.pgdas_membership, self.server, "allowed")
        self.client.force_login(self.admin)

    def test_employee_pages_visible_and_admin_not_exposed(self):
        for name in ("employee_list", "employee_add", "organization_edit", "password_change"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200, name)
        self.assertContains(self.client.get(reverse("server_list")), "Employees")
        self.assertEqual(self.client.get("/admin/").status_code, 404)

    def test_create_employee_password_hashed_and_role_saved(self):
        response = self.client.post(reverse("employee_add"), {"name": "New Person", "designation": "Analyst", "username": "newperson",
            "email": "new@example.test", "role": "viewer", "is_active": "on", "password": "Sample-safe-9381!", "password_confirm": "Sample-safe-9381!"})
        self.assertEqual(response.status_code, 302)
        user = get_user_model().objects.get(email="new@example.test")
        self.assertTrue(user.check_password("Sample-safe-9381!"))
        self.assertEqual(user.display_name, "New Person")
        self.assertEqual(user.pgdas_membership.role, "viewer")
        self.assertFalse(user.is_staff)

    def test_last_admin_cannot_be_deactivated(self):
        response = self.client.post(reverse("employee_edit", args=[self.admin.pk]), {"name": "Admin", "email": "admin@example.test", "role": "viewer"})
        self.assertContains(response, "Keep at least one active Super User")
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)
        self.assertEqual(self.admin.pgdas_membership.role, "super_user")

    def test_settings_do_not_render_saved_execution_password(self):
        grant = DatabaseGrant.objects.get(membership=self.editor.pgdas_membership)
        response = self.client.get(reverse("employee_permissions", args=[self.editor.pk]), {"grant": grant.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Restricted PostgreSQL login")

    def test_last_super_user_cannot_be_demoted_or_deactivated_separately(self):
        for changes in ({"role": "viewer", "is_active": "on"}, {"role": "super_user"}):
            with self.subTest(changes=changes):
                response = self.client.post(reverse("employee_edit", args=[self.admin.pk]), {
                    "name": "Administrator", "email": "admin@example.test", **changes})
                self.assertContains(response, "Keep at least one active Super User")
                self.admin.refresh_from_db()
                self.assertTrue(self.admin.is_active)
                self.assertEqual(Membership.objects.get(user=self.admin).role, "super_user")

    def test_super_user_can_be_demoted_when_another_active_super_user_remains(self):
        Membership.objects.filter(user=self.editor).update(role="super_user")
        response = self.client.post(reverse("employee_edit", args=[self.admin.pk]), {
            "name": "Administrator", "email": "admin@example.test", "role": "viewer", "is_active": "on"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Membership.objects.get(user=self.admin).role, "viewer")
        self.assertTrue(Membership.objects.filter(organization=self.org, role="super_user", user__is_active=True).exists())

    @patch("explorer.pg._open")
    def test_unassigned_database_and_admin_routes_blocked_before_connect(self, connect):
        self.client.force_login(self.editor)
        for name in ("employee_list", "server_add", "monitor", "watch"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)
        self.assertEqual(self.client.get(reverse("detail"), {"type": "table", "server": self.server.pk, "db": "payroll", "schema": "public", "table": "people"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("sql_runner", args=[self.server.pk]), {"db": "payroll"}).status_code, 403)
        connect.assert_not_called()

    @patch("explorer.changes.execute")
    def test_write_endpoint_checks_action_even_if_staff_bit_set(self, execute):
        self.client.force_login(self.editor)
        response = self.client.post(reverse("write_execute", args=[self.server.pk]), json.dumps({"db": "allowed", "sql": "DELETE FROM people"}), content_type="application/json")
        self.assertEqual(response.status_code, 403)
        execute.assert_not_called()

    @patch("explorer.pg.run_sql")
    def test_read_only_transaction_escape_and_cte_writes_blocked(self, run):
        self.client.force_login(self.viewer)
        for text in ("COMMIT; BEGIN READ WRITE; DELETE FROM people; COMMIT", "SET transaction_read_only=off", "WITH x AS (DELETE FROM people RETURNING *) SELECT * FROM x", "SELECT dblink_exec('secret', 'DELETE FROM people')"):
            response = self.client.post(reverse("sql_runner", args=[self.server.pk]), {"db": "allowed", "sql": text})
            self.assertEqual(response.status_code, 403, text)
        run.assert_not_called()

    def test_sql_parser_handles_comments_quotes_and_independent_permissions(self):
        authorize(self.editor, self.server, "allowed", "/* SELECT fake */ INSERT INTO people(name) VALUES ('DELETE; DROP')", write=True)
        authorize(self.viewer, self.server, "allowed", "SELECT count(*) FROM public.people")
        with self.assertRaises(PermissionDenied):
            authorize(self.editor, self.server, "allowed", "INSERT INTO people VALUES (1) ON CONFLICT(id) DO UPDATE SET id=2", write=True)
        with self.assertRaises(PermissionDenied):
            authorize(self.editor, self.server, "allowed", "CREATE ROLE bad SUPERUSER", write=True)

    def test_existing_admin_cannot_reopen_setup(self):
        self.assertEqual(self.client.get(reverse("setup")).status_code, 404)

    def test_new_database_has_no_implicit_employee_access(self):
        from access_control.policy import permits
        self.assertFalse(permits(self.editor, "read", server=self.server, database="new_database"))

    def test_revoked_assignment_hides_history(self):
        from audit.models import record
        record(self.viewer, "sql.run", self.server, "SELECT 42", payload={"database": "allowed"})
        self.client.force_login(self.viewer)
        self.assertContains(self.client.get(reverse("audit_list")), "SELECT 42")
        DatabaseGrant.objects.filter(membership=self.viewer.pgdas_membership).delete()
        self.assertNotContains(self.client.get(reverse("audit_list")), "SELECT 42")

    def test_other_organization_employee_cannot_be_edited(self):
        other = Organization.objects.create(name="Other")
        Membership.objects.filter(user=self.viewer).update(organization=other)
        self.assertEqual(self.client.get(reverse("employee_edit", args=[self.viewer.pk])).status_code, 404)

    def test_first_install_requires_token_and_locks_after_success(self):
        import os
        Installation.objects.filter(pk=1).update(completed=False)
        self.client.logout()
        self.assertRedirects(self.client.get(reverse("login")), reverse("setup"))
        data = {"name": "Installer", "email": "installer@example.test", "organization": "Fresh", "password": "Setup-pass-9472!", "password_confirm": "Setup-pass-9472!", "installation_token": "wrong"}
        with patch.dict(os.environ, {"PGDAS_SETUP_TOKEN": "test-setup-token"}):
            self.assertContains(self.client.post(reverse("setup"), data), "Invalid installation token")
            data["installation_token"] = "test-setup-token"
            response = self.client.post(reverse("setup"), data)
            self.assertEqual(response.status_code, 302)
        self.assertTrue(Installation.objects.get(pk=1).completed)
        self.assertEqual(self.client.get(reverse("setup")).status_code, 404)

    def test_missing_execution_credentials_fail_closed(self):
        from access_control import runtime
        from explorer.pg import PgError
        from types import SimpleNamespace
        token = runtime.request_context.set(SimpleNamespace(user=self.editor))
        try:
            result = runtime.execution_server(self.server, "allowed")           # no restricted login: PGDAS is the gate, the server login is used
            self.assertTrue(result.pgdas_enforced)
            self.assertEqual(result.username, self.server.username)
        finally:
            runtime.request_context.reset(token)

    def test_edit_has_no_password_or_user_id_and_ignores_injected_password(self):
        response = self.client.get(reverse("employee_edit", args=[self.editor.pk]))
        self.assertNotContains(response, 'id="id_username"')
        self.assertNotContains(response, 'id="id_password"')
        self.assertNotContains(response, 'id="id_password_confirm"')
        old_password = self.editor.password
        response = self.client.post(reverse("employee_edit", args=[self.editor.pk]), {
            "name": "Editor renamed", "designation": "Manager", "email": "EDITOR@EXAMPLE.TEST", "role": "editor", "is_active": "on",
            "username": "injected", "password": "injected", "password_confirm": "injected"})
        self.assertEqual(response.status_code, 302)
        self.editor.refresh_from_db()
        self.assertEqual(self.editor.password, old_password)
        self.assertEqual(self.editor.email, "editor@example.test")
        self.assertEqual(self.editor.username, "editor@example.test")
        self.assertTrue(self.client.login(username="EDITOR@EXAMPLE.TEST", password="test-pass"))
