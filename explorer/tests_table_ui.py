"""Table structure buttons: staff get New table, Rename, Drop and the column actions; everyone else gets none of them."""
import json
import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from .tests import admin_connect
from .tests_admin import AdminFixture, DB

BASE_DIR = Path(__file__).resolve().parent.parent


class TableButtonsTests(AdminFixture, TestCase):
    def setUp(self):
        super().setUp()
        with admin_connect("postgres") as admin:
            admin.execute(f"CREATE DATABASE {DB}")
        with admin_connect(DB) as conn:
            conn.execute("CREATE TABLE people (id serial PRIMARY KEY, name text NOT NULL, age int)")
        self.staff_client = Client()
        self.staff_client.force_login(self.staff)
        self.plain = Client()
        self.plain.force_login(get_user_model().objects.create_user("plain", password="pw-12345-abc"))

    def page(self, client, **params):
        return client.get(reverse("detail"), {"server": self.server.pk, "db": DB, "schema": "public", **params}).content.decode()

    def structure(self, html):
        match = re.search(r'data-structure="([^"]*)"', html)
        return match and json.loads(match.group(1).replace("&quot;", '"'))

    def test_the_tables_folder_and_the_schema_page_offer_new_table(self):
        for kind in ("tables", "schema"):
            with self.subTest(kind=kind):
                html = self.page(self.staff_client, type=kind)
                self.assertEqual(self.structure(html), {"server": self.server.pk, "db": DB, "schema": "public", "table": ""})
                self.assertIn('data-table-dialog="new-table"', html)
                self.assertNotIn('data-table-dialog="new-table"', self.page(self.plain, type=kind))

    def test_the_table_page_offers_rename_and_drop_and_the_columns_tab_the_column_actions(self):
        html = self.page(self.staff_client, type="table", table="people")
        self.assertEqual(self.structure(html)["table"], "people")
        for needle in ('data-table-dialog="rename-table"', 'data-table-dialog="drop-table"'):
            self.assertIn(needle, html)
        columns = self.page(self.staff_client, type="table", table="people", tab="columns")
        for needle in ('data-table-dialog="add-column"', 'data-table-dialog="edit-column"', 'data-table-dialog="rename-column"', 'data-table-dialog="drop-column"',
                       'data-col-move="-1"', 'data-col-move="1"', "data-order-apply", "data-order-undo", 'data-columns-tab="[&quot;id&quot;, &quot;name&quot;, &quot;age&quot;]"',
                       'data-column="name"', 'data-nullable="0"'):
            self.assertIn(needle, columns)
        self.assertEqual(len(re.findall(r"<tr data-column=", columns)), 3)

    def test_people_who_are_not_staff_get_no_buttons_but_still_see_the_columns(self):
        for params in ({"type": "table", "table": "people"}, {"type": "table", "table": "people", "tab": "columns"}):
            html = self.page(self.plain, **params)
            for forbidden in ("data-table-dialog", "data-structure", "data-col-move", "data-order-apply"):
                self.assertNotIn(forbidden, html)
        self.assertIn("<td>name</td>", self.page(self.plain, type="table", table="people", tab="columns"))

    def test_views_and_sequences_have_no_table_buttons(self):
        with admin_connect(DB) as conn:
            conn.execute("CREATE VIEW people_names AS SELECT name FROM people")
        html = self.page(self.staff_client, type="view", table="people_names", tab="columns")
        self.assertNotIn("data-table-dialog", html)
        self.assertNotIn('data-table-dialog="new-table"', self.page(self.staff_client, type="views"))

    def test_the_script_and_styles_are_loaded_for_staff_only_and_use_no_html_or_browser_box(self):
        staff_home = self.staff_client.get("/").content.decode()
        plain_home = self.plain.get("/").content.decode()
        self.assertIn('src="/static/table_dialogs.js"', staff_home)
        self.assertIn('href="/static/tables.css"', staff_home)
        self.assertNotIn("table_dialogs.js", plain_home)
        script = (BASE_DIR / "static" / "table_dialogs.js").read_text(encoding="utf-8")
        for needle in ("REORDER COLUMNS (", "pgdasReview.start", "DROP TABLE ", "DROP COLUMN ", "RENAME COLUMN ", "ADD COLUMN ", "CREATE TABLE ", "SET NOT NULL", "DROP DEFAULT",
                       "USING ", "pgdas:changed", "data-order-apply"):
            self.assertIn(needle, script)
        for official in ("bigint", "bigserial", "bit varying", "boolean", "box", "bytea", "character varying", "cidr", "circle", "date", "double precision", "inet",
                         "integer", "interval", "json", "jsonb", "line", "lseg", "macaddr", "macaddr8", "money", "numeric", "path", "pg_lsn", "pg_snapshot", "point",
                         "polygon", "real", "smallint", "smallserial", "serial", "text", "time with time zone", "timestamp with time zone", "tsquery", "tsvector",
                         "uuid", "xml"):                                          # Table 8.1 of the PostgreSQL manual, "Data Types"
            self.assertIn(f'["{official}', script)
        self.assertNotIn("innerHTML", script)
        self.assertNotRegex(script, r"\bconfirm\(")
        explorer = (BASE_DIR / "static" / "explorer.js").read_text(encoding="utf-8")
        self.assertIn("change.open", explorer)
