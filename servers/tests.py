from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from audit.models import AuditEntry

from . import crypto
from .models import ServerConnection

FORM = {"name": "prod", "host": "db.example.test", "port": 5432, "database": "app", "username": "reader",
        "password": "s3cret-value", "sslmode": "require"}


class CryptoTests(TestCase):
    def test_round_trip_and_not_plain_text(self):
        token = crypto.encrypt("s3cret-value")
        self.assertNotIn("s3cret-value", token)
        self.assertEqual(crypto.decrypt(token), "s3cret-value")

    def test_wrong_key_gives_a_clear_error(self):
        token = crypto.encrypt("s3cret-value")
        with override_settings(SECRET_KEY="a-different-secret-key"), self.assertRaisesMessage(ValueError, "cannot be decrypted"):
            crypto.decrypt(token)


class ServerViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user("admin", password="pw-12345-abc", is_staff=True)
        self.viewer = User.objects.create_user("viewer", password="pw-12345-abc")

    def test_viewer_cannot_add_edit_or_delete(self):
        server = ServerConnection.objects.create(name="x", host="h", username="u")
        self.client.force_login(self.viewer)
        for url in (reverse("server_add"), reverse("server_edit", args=[server.pk]), reverse("server_delete", args=[server.pk])):
            self.assertEqual(self.client.get(url).status_code, 302, url)
            self.assertEqual(self.client.post(url, FORM).status_code, 302, url)
        self.assertEqual(ServerConnection.objects.count(), 1)

    def test_staff_adds_server_password_is_encrypted_and_audited(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse("server_add"), FORM)
        self.assertRedirects(response, reverse("server_list"))
        server = ServerConnection.objects.get(name="prod")
        self.assertNotIn("s3cret-value", server.password_encrypted)
        self.assertEqual(server.get_password(), "s3cret-value")
        entry = AuditEntry.objects.get(action="server.add")
        self.assertEqual((entry.username, entry.server_name), ("admin", "prod"))
        self.assertNotIn("s3cret-value", entry.detail)

    def test_password_required_on_add_but_optional_on_edit(self):
        self.client.force_login(self.staff)
        self.assertContains(self.client.post(reverse("server_add"), {**FORM, "password": ""}), "Enter the password")
        self.client.post(reverse("server_add"), FORM)
        server = ServerConnection.objects.get(name="prod")
        self.client.post(reverse("server_edit", args=[server.pk]), {**FORM, "host": "new.example.test", "password": ""})
        server.refresh_from_db()
        self.assertEqual((server.host, server.get_password()), ("new.example.test", "s3cret-value"))

    def test_password_never_rendered_back_in_edit_form(self):
        self.client.force_login(self.staff)
        self.client.post(reverse("server_add"), FORM)
        server = ServerConnection.objects.get(name="prod")
        page = self.client.get(reverse("server_edit", args=[server.pk]))
        self.assertNotContains(page, "s3cret-value")
        self.assertNotContains(page, server.password_encrypted)

    def test_audit_log_is_staff_only(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("audit_list")).status_code, 302)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("audit_list")).status_code, 200)

    def test_audit_page_lists_each_entry_with_its_details(self):
        # Regression: a template variable named "page" once hid the paginator and rendered blank rows.
        from audit.models import record
        record(self.staff, "sql.run", None, "SELECT 1 FROM audit_check", ok=True, rows=1)
        record(self.staff, "sql.run", None, "DELETE FROM audit_check", ok=False)
        self.client.force_login(self.staff)
        page = self.client.get(reverse("audit_list"))
        self.assertContains(page, "SELECT 1 FROM audit_check")
        self.assertContains(page, "DELETE FROM audit_check")
        self.assertContains(page, "failed")
        self.assertContains(page, "Page 1 of 1")
        self.assertEqual(page.content.decode().count("<tr>"), 3)  # header + two entries

    def test_navigation_highlights_the_current_section(self):
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse("audit_list")), 'href="/audit/" class="active"')
        self.assertNotContains(self.client.get(reverse("server_list")), 'href="/audit/" class="active"')

    def test_delete_keeps_audit_history(self):
        self.client.force_login(self.staff)
        self.client.post(reverse("server_add"), FORM)
        server = ServerConnection.objects.get(name="prod")
        self.client.post(reverse("server_delete", args=[server.pk]))
        self.assertFalse(ServerConnection.objects.exists())
        self.assertEqual(AuditEntry.objects.filter(server_name="prod").count(), 2)
