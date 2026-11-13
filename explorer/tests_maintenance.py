"""VACUUM / ANALYZE / REINDEX through the Maintenance permission, against the scratch PostgreSQL database."""
import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from access_control.models import Installation, Membership, Organization, OrganizationServer
from access_control.policy import preset_permissions
from access_control.services import assign_database
from audit.models import AuditEntry

from . import pg
from . import tests as shared


class Base(shared.PgFixture, TestCase):
    def setUp(self):
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Org")
        self.admin = get_user_model().objects.create_user("a", email="a@o.test", password="pw-12345-abc", is_staff=True, is_superuser=True)
        Membership.objects.create(user=self.admin, organization=self.org, role="super_user")
        self.server = shared.make_server()
        OrganizationServer.objects.create(server=self.server, organization=self.org)
        self.db = shared.TARGET["NAME"]
        self.person = get_user_model().objects.create_user("p", email="p@o.test", password="pw-12345-abc")
        self.member = Membership.objects.create(user=self.person, organization=self.org, role="editor")

    def run_op(self, user, operation="vacuum", table="people"):
        client = Client()
        client.force_login(user)
        return client.post(reverse("maintenance_run", args=[self.server.pk]), json.dumps({"db": self.db, "schema": "pgdas_demo", "table": table, "operation": operation}),
                           content_type="application/json")

class MaintenanceTests(Base):
    def test_every_operation_runs_for_a_super_user_and_is_audited(self):
        for operation in ("vacuum", "analyze", "vacuum_analyze", "reindex"):
            with self.subTest(operation=operation):
                response = self.run_op(self.admin, operation)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertTrue(response.json()["ok"])
        self.assertTrue(AuditEntry.objects.filter(action="maintenance.vacuum", ok=True).exists())

    def test_without_the_permission_it_is_refused_with_the_pgdas_message(self):
        assign_database(self.admin, self.member, self.server, self.db, preset_permissions("designer"))          # a Designer still needs Maintenance ticked
        response = self.run_op(self.person)
        self.assertEqual(response.status_code, 403)
        self.assertIn("PGDAS blocked this", response.json()["error"])
        self.assertIn("maintenance", response.json()["error"])

    def test_with_the_permission_an_employee_can_run_it(self):
        assign_database(self.admin, self.member, self.server, self.db, {**preset_permissions("viewer"), "maintenance": True})
        self.assertEqual(self.run_op(self.person, "analyze").status_code, 200)
        self.assertEqual(self.run_op(self.person, "reindex").status_code, 200)

    def test_another_database_is_not_covered(self):
        assign_database(self.admin, self.member, self.server, "some_other_db", {**preset_permissions("viewer"), "maintenance": True})
        self.assertEqual(self.run_op(self.person).status_code, 403)

    def test_an_unknown_operation_and_a_missing_table_are_answered_plainly(self):
        self.assertEqual(self.run_op(self.admin, "drop").status_code, 400)
        missing = self.run_op(self.admin, "vacuum", table="no_such_table")
        self.assertEqual(missing.status_code, 400)
        self.assertFalse(missing.json()["ok"])
        self.assertTrue(AuditEntry.objects.filter(action="maintenance.vacuum", ok=False).exists())

    def test_a_table_name_cannot_smuggle_a_second_statement(self):
        response = self.run_op(self.admin, "vacuum", table='people; DROP TABLE pgdas_demo.people')
        self.assertEqual(response.status_code, 400)                                  # it is quoted as one identifier, which does not exist
        with shared.admin_connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM pgdas_demo.people").fetchone()[0], 120)

    def test_the_buttons_appear_on_a_table_page_only_with_the_permission(self):
        node = {"type": "table", "server": self.server.pk, "db": self.db, "schema": "pgdas_demo", "table": "people", "system": 0}
        client = Client()
        client.force_login(self.person)
        assign_database(self.admin, self.member, self.server, self.db, preset_permissions("viewer"))
        url = reverse("detail") + "?" + "&".join(f"{k}={v}" for k, v in node.items())
        self.assertNotContains(client.get(url), "data-maint=")
        assign_database(self.admin, self.member, self.server, self.db, {**preset_permissions("viewer"), "maintenance": True})
        self.assertContains(client.get(url), 'data-maint="vacuum"')

class Writing(Base):
    """The review popup's endpoints, for the tests below."""

    def write(self, user, sql):
        client = Client()
        client.force_login(user)
        return client.post(reverse("write_execute", args=[self.server.pk]), json.dumps({"db": self.db, "sql": sql}), content_type="application/json")

    def flow(self, user, sql):
        """The popup's two steps: preview, then execute with what the preview reported and the word it asked to be typed."""
        client = Client()
        client.force_login(user)
        body = json.dumps({"db": self.db, "sql": sql})
        preview = client.post(reverse("write_preview", args=[self.server.pk]), body, content_type="application/json")
        if preview.status_code != 200:
            return preview
        shown = preview.json()
        return client.post(reverse("write_execute", args=[self.server.pk]),
                           json.dumps({"db": self.db, "sql": sql, "fingerprint": shown.get("fingerprint"), "confirm": shown.get("confirm_word")}),
                           content_type="application/json")

    def count(self):
        with shared.admin_connect(self.db) as conn:
            return conn.execute("SELECT count(*) FROM pgdas_demo.people").fetchone()[0]

class FreeFormWriteTests(Writing):
    """A person with the right level changes data through the review popup's endpoints; a viewer cannot."""

    def test_an_editor_can_insert_and_a_viewer_cannot(self):
        assign_database(self.admin, self.member, self.server, self.db, preset_permissions("editor"))
        before = self.count()
        response = self.write(self.person, "INSERT INTO pgdas_demo.people (name) VALUES ('typed by an editor')")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.count(), before + 1)
        assign_database(self.admin, self.member, self.server, self.db, preset_permissions("viewer"))
        refused = self.write(self.person, "INSERT INTO pgdas_demo.people (name) VALUES ('typed by a viewer')")
        self.assertEqual(refused.status_code, 403)
        self.assertIn("PGDAS blocked this", refused.json()["error"])
        self.assertEqual(self.count(), before + 1)

    def test_an_editor_cannot_drop_and_a_designer_can_create(self):
        assign_database(self.admin, self.member, self.server, self.db, preset_permissions("editor"))
        self.assertEqual(self.flow(self.person, "DROP TABLE pgdas_demo.items").status_code, 403)
        self.assertEqual(self.flow(self.person, "CREATE TABLE pgdas_demo.editor_made (id int)").status_code, 403)
        assign_database(self.admin, self.member, self.server, self.db, preset_permissions("designer"))
        created = self.flow(self.person, "CREATE TABLE pgdas_demo.designer_made (id int)")
        self.assertEqual(created.status_code, 200, created.content)
        with shared.admin_connect(self.db) as conn:
            conn.execute("DROP TABLE IF EXISTS pgdas_demo.designer_made")

    def test_a_postgresql_refusal_is_reported_as_the_second_gate(self):
        from unittest.mock import patch
        import psycopg
        assign_database(self.admin, self.member, self.server, self.db, preset_permissions("editor"))
        with patch("explorer.pg.psycopg.connect", side_effect=psycopg.errors.InsufficientPrivilege("permission denied for table people")):
            response = self.write(self.person, "INSERT INTO pgdas_demo.people (name) VALUES ('x')")
        self.assertEqual(response.status_code, 400)
        self.assertIn("PostgreSQL blocked this", response.json()["error"])


class StructureDialogTests(Writing):
    """Constraints, column types and defaults, partitions and column order are open to a Designer, not to an Editor; owners and privileges are not."""

    def setUp(self):
        super().setUp()
        with shared.admin_connect(self.db) as conn:
            for statement in ("DROP TABLE IF EXISTS pgdas_demo.pt CASCADE", "DROP TABLE IF EXISTS pgdas_demo.pt_1", "DROP TABLE IF EXISTS pgdas_demo.reo",
                              "CREATE TABLE pgdas_demo.pt (id int, v text) PARTITION BY RANGE (id)",
                              "CREATE TABLE pgdas_demo.pt_1 (id int, v text)",
                              "CREATE TABLE pgdas_demo.reo (a int, b text, c int)", "INSERT INTO pgdas_demo.reo VALUES (1, 'x', 3), (2, 'y', 4)"):
                conn.execute(statement)
        self.addCleanup(self.drop_scratch)

    def drop_scratch(self):
        with shared.admin_connect(self.db) as conn:
            for statement in ("DROP TABLE IF EXISTS pgdas_demo.pt CASCADE", "DROP TABLE IF EXISTS pgdas_demo.pt_1", "DROP TABLE IF EXISTS pgdas_demo.reo"):
                conn.execute(statement)

    def columns(self, table):
        with shared.admin_connect(self.db) as conn:
            return [r[0] for r in conn.execute("SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
                                               (f"pgdas_demo.{table}",)).fetchall()]

    def test_constraints_types_defaults_and_partitions_for_a_designer(self):
        assign_database(self.admin, self.member, self.server, self.db, preset_permissions("designer"))
        for sql in ("ALTER TABLE pgdas_demo.items ADD CONSTRAINT items_price_pos CHECK (price >= 0)",
                    "ALTER TABLE pgdas_demo.items DROP CONSTRAINT items_price_pos",
                    "ALTER TABLE pgdas_demo.people ALTER COLUMN note SET DEFAULT 'none'",
                    "ALTER TABLE pgdas_demo.people ALTER COLUMN note DROP DEFAULT",
                    "ALTER TABLE pgdas_demo.orders ALTER COLUMN amount TYPE numeric(12, 2)",
                    "ALTER TABLE pgdas_demo.pt ATTACH PARTITION pgdas_demo.pt_1 FOR VALUES FROM (1) TO (10)",
                    "ALTER TABLE pgdas_demo.pt DETACH PARTITION pgdas_demo.pt_1"):
            with self.subTest(sql=sql):
                response = self.flow(self.person, sql)
                self.assertEqual(response.status_code, 200, response.content)

    def test_an_editor_is_refused_those_with_the_pgdas_message(self):
        assign_database(self.admin, self.member, self.server, self.db, preset_permissions("editor"))
        for sql in ("ALTER TABLE pgdas_demo.items ADD CONSTRAINT c CHECK (price >= 0)", "ALTER TABLE pgdas_demo.orders ALTER COLUMN amount TYPE numeric(12, 2)",
                    "ALTER TABLE pgdas_demo.pt ATTACH PARTITION pgdas_demo.pt_1 FOR VALUES FROM (1) TO (10)"):
            with self.subTest(sql=sql):
                response = self.flow(self.person, sql)
                self.assertEqual(response.status_code, 403)
                self.assertIn("PGDAS blocked this", response.json()["error"])

    def test_owner_row_security_and_a_function_in_a_default_are_still_for_super_users(self):
        assign_database(self.admin, self.member, self.server, self.db, preset_permissions("designer"))
        for sql in ("ALTER TABLE pgdas_demo.people OWNER TO postgres", "ALTER TABLE pgdas_demo.people ENABLE ROW LEVEL SECURITY",
                    "ALTER TABLE pgdas_demo.people ALTER COLUMN note SET DEFAULT pg_read_file('/etc/passwd')"):
            with self.subTest(sql=sql):
                self.assertEqual(self.flow(self.person, sql).status_code, 403)

    def test_changing_the_column_order_needs_create_alter_drop_and_insert(self):
        assign_database(self.admin, self.member, self.server, self.db, preset_permissions("editor"))
        self.assertEqual(self.flow(self.person, 'ALTER TABLE pgdas_demo.reo REORDER COLUMNS ("c", "a", "b")').status_code, 403)
        assign_database(self.admin, self.member, self.server, self.db, preset_permissions("designer"))
        response = self.flow(self.person, 'ALTER TABLE pgdas_demo.reo REORDER COLUMNS ("c", "a", "b")')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.columns("reo"), ["c", "a", "b"])