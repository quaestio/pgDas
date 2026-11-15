"""The settings that explain the charts: the explanation and the manual link for the server's version, and changing one (Super User only)."""
import json
from pathlib import Path

from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from access_control.models import Installation, Membership, Organization, OrganizationServer
from audit.models import AuditEntry
from explorer import tests as shared
from explorer.tests import admin_connect

from . import metrics, server_settings
from .models import ServerHealth


class ManualLinkTests(TestCase):
    def test_the_link_names_the_servers_own_version(self):
        self.assertEqual(server_settings.docs_url("work_mem", 180000), "https://www.postgresql.org/docs/18/runtime-config-resource.html#GUC-WORK-MEM")
        self.assertEqual(server_settings.docs_url("work_mem", 120005), "https://www.postgresql.org/docs/12/runtime-config-resource.html#GUC-WORK-MEM")
        self.assertIn("/docs/16/", server_settings.docs_url("wal_level", 160004))

    def test_autovacuum_moved_to_the_vacuum_page_in_18(self):
        self.assertIn("runtime-config-vacuum.html#GUC-AUTOVACUUM-FREEZE-MAX-AGE", server_settings.docs_url("autovacuum_freeze_max_age", 180001))
        self.assertIn("/17/runtime-config-autovacuum.html#GUC-AUTOVACUUM", server_settings.docs_url("autovacuum", 170005))

    def test_every_setting_the_sampler_collects_is_explained(self):
        info = server_settings.describe(180000)
        for name in metrics.SETTINGS:
            self.assertIn(name, info)
            self.assertTrue(info[name]["text"])
            self.assertTrue(info[name]["url"].startswith("https://www.postgresql.org/docs/18/"))
        self.assertFalse(info["shared_preload_libraries"]["editable"])
        self.assertTrue(info["work_mem"]["editable"])


@override_settings(SERVER_NETWORKS="any", ALLOWED_SERVER_HOSTS=[])
class ChangeSettingTests(shared.PgFixture, TestCase):
    NAME = "log_min_duration_statement"          # harmless and needs only a reload

    def setUp(self):
        cache.clear()
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Org")
        self.admin = get_user_model().objects.create_user("a", email="a@o.test", password="pw-12345-abc", is_staff=True, is_superuser=True)
        Membership.objects.create(user=self.admin, organization=self.org, role="super_user")
        self.server = shared.make_server()
        OrganizationServer.objects.filter(server=self.server).delete()
        OrganizationServer.objects.create(server=self.server, organization=self.org)
        self.client = Client()
        self.client.force_login(self.admin)
        self.addCleanup(self.put_back)

    def put_back(self):                      # whatever a test did, leave the local server's configuration as it was
        with admin_connect("postgres") as conn:
            conn.autocommit = True
            conn.execute(f"ALTER SYSTEM RESET {self.NAME}")
            conn.execute("ALTER SYSTEM RESET work_mem")
            conn.execute("SELECT pg_reload_conf()")

    def post(self, name, url="setting_preview", client=None, **body):
        return (client or self.client).post(reverse(url), json.dumps({"server": self.server.pk, "name": name, **body}), content_type="application/json")

    def current(self, name):
        with admin_connect("postgres") as conn:
            return conn.execute("SELECT current_setting(%s)", (name,)).fetchone()[0]

    def test_the_review_says_what_it_is_now_and_what_it_would_become(self):
        data = self.post(self.NAME, value="5000").json()
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["confirm_word"], "ALTER")
        self.assertEqual(data["statement"], f'ALTER SYSTEM SET "{self.NAME}" = \'5000\'')
        self.assertIn("→ 5000", data["objects"][0]["text"])
        self.assertFalse(data["notes"][0]["bad"])                      # a reload is enough
        self.assertEqual(self.current(self.NAME), "-1")               # previewing changed nothing

    def test_a_setting_that_needs_a_restart_says_so(self):
        data = self.post("max_connections", value="150").json()
        self.assertTrue(data["notes"][0]["bad"])
        self.assertIn("restart", data["notes"][0]["text"])

    def test_values_that_cannot_be_right_are_refused_before_anything_is_saved(self):
        for name, value, message in (("work_mem", "lots", "number"), ("work_mem", "10 parsecs", "unit"), ("work_mem", "1", "outside"), ("work_mem", "0.5", "whole"),
                                     ("wal_level", "sideways", "one of"), ("autovacuum", "maybe", "on or off"), ("max_connections", "99999999999", "outside")):
            with self.subTest(name=name, value=value):
                response = self.post(name, value=value)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json()["error"])

    def test_only_the_listed_settings_can_be_changed_and_preload_libraries_are_read_only(self):
        self.assertEqual(self.post("fsync", value="off").status_code, 400)
        self.assertEqual(self.post("shared_preload_libraries", value="pg_stat_statements").status_code, 400)
        self.assertEqual(self.post(self.NAME, value="").status_code, 400)

    def test_a_setting_is_changed_only_with_the_word_and_is_recorded(self):
        data = self.post(self.NAME, value="5000").json()
        wrong = self.post(self.NAME, "setting_apply", value="5000", fingerprint=data["fingerprint"], confirm="alter")
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(self.current(self.NAME), "-1")
        done = self.post(self.NAME, "setting_apply", value="5000", fingerprint=data["fingerprint"], confirm="ALTER")
        self.assertEqual(done.status_code, 200, done.content)
        self.assertEqual(self.current(self.NAME), "5s")
        entry = AuditEntry.objects.filter(action="alter").latest("pk")
        self.assertIn(self.NAME, entry.detail)
        self.assertTrue(entry.ok)
        self.assertFalse(entry.can_undo)

    def test_going_back_to_the_default_removes_the_saved_value(self):
        first = self.post("work_mem", value="8MB").json()
        self.assertEqual(self.post("work_mem", "setting_apply", value="8MB", fingerprint=first["fingerprint"], confirm="ALTER").status_code, 200)
        self.assertEqual(self.current("work_mem"), "8MB")
        back = self.post("work_mem", reset=True).json()
        self.assertIn("the default", back["objects"][0]["text"])
        self.assertEqual(self.post("work_mem", "setting_apply", reset=True, fingerprint=back["fingerprint"], confirm="ALTER").status_code, 200)
        self.assertEqual(self.current("work_mem"), "4MB")

    def test_a_setting_that_changed_since_the_review_is_not_changed(self):
        data = self.post(self.NAME, value="5000").json()
        data["fingerprint"]["current"] = "1s"
        response = self.post(self.NAME, "setting_apply", value="5000", fingerprint=data["fingerprint"], confirm="ALTER")
        self.assertEqual(response.status_code, 400)
        self.assertIn("changed since", response.json()["error"])
        self.assertEqual(self.current(self.NAME), "-1")

    def test_only_a_super_user_may_change_settings(self):
        person = get_user_model().objects.create_user("p", email="p@o.test", password="pw-12345-abc", is_staff=True)
        Membership.objects.create(user=person, organization=self.org, role="editor")
        other = Client()
        other.force_login(person)
        for url in ("setting_preview", "setting_apply"):
            self.assertIn(self.post(self.NAME, url, client=other, value="5000").status_code, (302, 403))       # sent away, or refused outright
        self.assertEqual(self.current(self.NAME), "-1")

    def test_the_data_carries_the_explanations_and_whether_this_person_may_change_them(self):
        ServerHealth.objects.create(server=self.server, version_num=180000, version_text="PostgreSQL 18.0", sample_count=1, settings={"work_mem": "4MB"})
        payload = self.client.get(reverse("dashboards_data"), {"server": self.server.pk}).json()
        self.assertTrue(payload["can_edit_settings"])
        self.assertIn("GUC-WORK-MEM", payload["tables"]["settings_info"]["work_mem"]["url"])

    def test_the_script_has_the_explanation_link_and_change_button(self):
        script = (Path(django_settings.BASE_DIR) / "static" / "dashboards.js").read_text(encoding="utf-8")
        for needle in ("settingsPanel", "settings_info", "manual ↗", "changeSetting", "can_edit_settings", "data-setting-preview-url", "noopener"):
            self.assertIn(needle, script)
        self.assertNotIn("innerHTML", script)
