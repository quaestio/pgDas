"""Export a table as CSV, JSON or SQL, and the CSV import page wiring (the import itself is saved through the row editor, see tests_row_edits)."""
import csv
import io
import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, override_settings
from pgdas.testing import TestCase
from django.urls import reverse

from audit.models import AuditEntry

from . import exporter
from .tests import admin_connect
from .tests_admin import AdminFixture, DB

BASE_DIR = Path(__file__).resolve().parent.parent


class ExporterTests(SimpleTestCase):
    def test_csv_has_a_header_quotes_awkward_values_and_defuses_formulas(self):
        text = "".join(exporter.stream("csv", ["a", "b"], [("1", 'say "hi", ok'), (None, "=SUM(A1)"), ("x\ny", "-5")]))
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows, [["a", "b"], ["1", 'say "hi", ok'], ["", "'=SUM(A1)"], ["x\ny", "'-5"]])

    def test_json_is_one_array_of_objects_and_empty_is_valid(self):
        text = "".join(exporter.stream("json", ["id", "name"], [(1, "Ann"), (2, None)]))
        self.assertEqual(json.loads(text), [{"id": 1, "name": "Ann"}, {"id": 2, "name": None}])
        self.assertEqual(json.loads("".join(exporter.stream("json", ["id"], []))), [])

    def test_sql_makes_insert_statements_in_batches_with_numbers_bare_and_text_escaped(self):
        rows = [(str(n), f"it's {n}", "true") for n in range(exporter.BATCH + 1)]
        text = "".join(exporter.stream("sql", ["id", "note", "ok"], rows, ("public", "t"), ["integer", "text", "boolean"]))
        self.assertEqual(text.count("INSERT INTO"), 2)
        self.assertIn('INSERT INTO "public"."t" ("id", "note", "ok") VALUES\n  (0, \'it\'\'s 0\', true)', text)
        self.assertTrue(text.rstrip().endswith(";"))

    def test_sql_literal_quotes_what_is_not_a_clean_number_or_boolean(self):
        self.assertEqual(exporter.sql_literal("12", "integer"), "12")
        self.assertEqual(exporter.sql_literal("1; DROP TABLE x", "integer"), "'1; DROP TABLE x'")
        self.assertEqual(exporter.sql_literal("NaN", "numeric(10,2)"), "'NaN'")
        self.assertEqual(exporter.sql_literal(None, "text"), "NULL")
        self.assertEqual(exporter.sql_literal("t", "boolean"), "'t'")


class ExportViewTests(AdminFixture, TestCase):
    def setUp(self):
        super().setUp()
        with admin_connect("postgres") as admin:
            admin.execute(f"CREATE DATABASE {DB}")
        with admin_connect(DB) as conn:
            conn.execute("CREATE TABLE people (id int PRIMARY KEY, name text, born date, meta jsonb, ok boolean);"
                         "INSERT INTO people VALUES (1, 'Ann', '2020-01-02', '{\"a\": [1, 2]}', true), (2, '=cmd|x', NULL, NULL, false), (3, 'O''Neil', '1999-12-31', '{}', NULL);"
                         "CREATE VIEW names AS SELECT name FROM people")
        self.client = Client()
        self.client.force_login(self.staff)

    def get(self, fmt, table="people", client=None, **extra):
        params = {"db": DB, "schema": "public", "table": table, "format": fmt, **extra}
        return (client or self.client).get(reverse("table_export", args=[self.server.pk]), params)

    def body(self, response):
        return b"".join(response.streaming_content).decode()

    def test_csv_download_has_every_row_exact_text_and_a_file_name(self):
        response = self.get("csv")
        self.assertEqual((response.status_code, response["Content-Type"], response["Content-Disposition"]),
                         (200, "text/csv; charset=utf-8", 'attachment; filename="people.csv"'))
        rows = list(csv.reader(io.StringIO(self.body(response))))
        self.assertEqual(rows[0], ["id", "name", "born", "meta", "ok"])
        self.assertEqual(sorted(rows[1:]), sorted([["1", "Ann", "2020-01-02", '{"a": [1, 2]}', "true"], ["2", "'=cmd|x", "", "", "false"], ["3", "O'Neil", "1999-12-31", "{}", ""]]))

    def test_json_download_keeps_native_types(self):
        data = json.loads(self.body(self.get("json")))
        by_id = {r["id"]: r for r in data}
        self.assertEqual(by_id[1], {"id": 1, "name": "Ann", "born": "2020-01-02", "meta": {"a": [1, 2]}, "ok": True})
        self.assertIsNone(by_id[2]["meta"])

    def test_sql_download_loads_back_into_a_copy_of_the_table(self):
        text = self.body(self.get("sql"))
        with admin_connect(DB) as conn:
            conn.execute("CREATE TABLE copy (LIKE people INCLUDING ALL)")
            conn.execute(text.replace('"public"."people"', '"public"."copy"'))
            same = conn.execute("SELECT count(*) FROM (SELECT * FROM people EXCEPT SELECT * FROM copy) d").fetchone()[0]
            total = conn.execute("SELECT count(*) FROM copy").fetchone()[0]
        self.assertEqual((same, total), (0, 3))

    def test_views_can_be_exported_and_the_download_is_recorded(self):
        self.assertEqual(self.body(self.get("csv", table="names")).splitlines()[0], "name")
        entry = AuditEntry.objects.get(action="table.export")
        self.assertEqual((entry.rows, entry.payload["format"], entry.payload["table"]), (3, "csv", "names"))

    @override_settings(MAX_EXPORT_ROWS=2)
    def test_an_export_stops_at_the_row_limit(self):
        rows = list(csv.reader(io.StringIO(self.body(self.get("csv")))))
        self.assertEqual(len(rows), 3)                                                   # header + 2
        self.assertEqual(AuditEntry.objects.get(action="table.export").rows, 2)

    def test_bad_requests_and_missing_tables_are_refused_politely(self):
        self.assertEqual(self.get("xml").status_code, 400)
        self.assertEqual(self.get("csv", table="").status_code, 400)
        self.assertEqual(self.get("csv", table="nope").status_code, 404)
        self.assertEqual(Client().get(reverse("table_export", args=[self.server.pk]), {"db": DB, "schema": "public", "table": "people"}).status_code, 302)

    def test_the_data_tab_offers_the_three_formats(self):
        html = self.client.get(reverse("detail"), {"type": "table", "server": self.server.pk, "db": DB, "schema": "public", "table": "people", "tab": "data"}).content.decode()
        for fmt in ("csv", "json", "sql"):
            self.assertIn(f"&format={fmt}", html)
        self.assertIn("data-import-csv", html)
        plain = Client()
        plain.force_login(get_user_model().objects.create_user("pat", password="pw-12345-abc"))
        html = plain.get(reverse("detail"), {"type": "table", "server": self.server.pk, "db": DB, "schema": "public", "table": "people", "tab": "data"}).content.decode()
        self.assertNotIn("data-import-csv", html)

    def test_importing_many_rows_goes_through_the_row_editor_in_one_undoable_save(self):
        batch = {"schema": "public", "table": "people", "edits": [], "deletes": [],
                 "inserts": [{"id": str(100 + n), "name": f"n{n}", "born": None} for n in range(300)]}
        preview = self.client.post(reverse("rows_preview", args=[self.server.pk]), json.dumps({"db": DB, **batch}), content_type="application/json")
        self.assertEqual(preview.status_code, 200, preview.content)
        data = preview.json()
        self.assertEqual((data["count"], len(data["objects"])), (300, 200))             # all are saved, the popup lists the first 200
        saved = self.client.post(reverse("rows_execute", args=[self.server.pk]), json.dumps({"db": DB, **batch, "fingerprint": data["fingerprint"]}), content_type="application/json")
        self.assertEqual(saved.status_code, 200, saved.content)
        with admin_connect(DB) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM people").fetchone()[0], 303)
        entry = AuditEntry.objects.get(action="rows.edit")
        self.client.post(reverse("audit_undo", args=[entry.pk]))
        with admin_connect(DB) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM people").fetchone()[0], 3)

    def test_the_import_script_is_wired_and_safe(self):
        self.assertIn('src="/static/table_io.js"', self.client.get("/").content.decode())
        script = (BASE_DIR / "static" / "table_io.js").read_text(encoding="utf-8")
        for needle in ("parseCsv", "FileReader", "pgdasReview.start", "inserts: rows", "MAX_ROWS = 5000", "Empty cells become NULL", "guessDelimiter"):
            self.assertIn(needle, script)
        self.assertNotIn("innerHTML", script)
        self.assertNotRegex(script, r"\bconfirm\(")
