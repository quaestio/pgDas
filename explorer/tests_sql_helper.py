"""The SQL runner's helpers: tables and columns for the side panel, hints and the query builder."""
import json
from pathlib import Path

from django.urls import reverse

from .tests import admin_connect
from .tests_admin import DB
from .tests_table_script import ScriptFixture

BASE_DIR = Path(__file__).resolve().parent.parent


class SqlHelperTests(ScriptFixture):
    def setUp(self):
        super().setUp()
        with admin_connect(DB) as conn:
            conn.execute("CREATE TABLE customers (id serial PRIMARY KEY, name text, balance numeric(10,2));"
                         "CREATE VIEW rich AS SELECT id, name FROM customers WHERE balance > 100")

    def tables(self, **params):
        return self.client.get(reverse("database_tables", args=[self.server.pk]), {"db": DB, **params}).json()["tables"]

    def test_the_panel_lists_tables_and_columns_and_with_views_also_the_views(self):
        plain = {t["name"]: t for t in self.tables()}
        self.assertIn("customers", plain)
        self.assertNotIn("rich", plain)                                                  # the constraint dialogs still see tables only
        every = {t["name"]: t for t in self.tables(views="1")}
        self.assertEqual(every["rich"]["kind"], "view")
        self.assertEqual([c["name"] for c in every["customers"]["columns"]], ["id", "name", "balance"])
        self.assertEqual(every["customers"]["columns"][2]["type"], "numeric(10,2)")

    def test_the_runner_page_has_the_panel_the_hint_strip_and_the_scripts(self):
        page = self.client.get(reverse("sql_runner", args=[self.server.pk]), {"db": DB})
        for needle in ('id="sqlHelper"', 'id="sqlSuggest"', "sql_helper.js", "sql_helper.css", f"db={DB}", "views=1"):
            self.assertContains(page, needle)

    def test_the_script_builds_hints_columns_and_a_select_and_puts_text_not_html(self):
        script = (BASE_DIR / "static" / "sql_helper.js").read_text(encoding="utf-8")
        for needle in ("Tables and columns", "Query builder", "Put it in the SQL box", "setRangeText", "Ctrl+Space", "WHERE ", "ORDER BY ", "LIMIT ", "valueSql", "RESERVED"):
            self.assertIn(needle, script)
        self.assertNotIn("innerHTML", script)

    def test_the_scripts_are_valid_javascript(self):
        import shutil, subprocess
        if not shutil.which("node"):
            self.skipTest("needs Node.js")
        for name in ("sql_helper.js", "sql_run.js", "sql_highlight.js"):
            done = subprocess.run(["node", "--check", str(BASE_DIR / "static" / name)], capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, name + done.stderr)

    def test_clicking_a_table_writes_a_select_and_the_box_is_coloured_and_runs_in_ajax(self):
        helper = (BASE_DIR / "static" / "sql_helper.js").read_text(encoding="utf-8")
        for needle in ("SELECT * FROM ", " LIMIT 10;", "putTable", "statementAt", "indexOf(\"*\")"):
            self.assertIn(needle, helper)
        highlight = (BASE_DIR / "static" / "sql_highlight.js").read_text(encoding="utf-8")
        for needle in ("sqlk-reserved", "sqlk-string", "sqlk-comment", "pgdasSqlWords", "aria-hidden"):
            self.assertIn(needle, highlight)
        run = (BASE_DIR / "static" / "sql_run.js").read_text(encoding="utf-8")
        for needle in ("data-run-url", "data-page-url", "sqlr-foot", "sqlr-export", "sqlr-stats", "Ctrl", "canWrite"):
            self.assertIn(needle, run)
        for script in (helper, highlight, run):
            self.assertNotIn("innerHTML", script)
        page = self.client.get(reverse("sql_runner", args=[self.server.pk]), {"db": DB})
        for needle in ("sql_highlight.js", "sql_run.js", 'id="sqlAjax"', reverse("sql_run", args=[self.server.pk]), reverse("sql_page", args=[self.server.pk])):
            self.assertContains(page, needle)


class SqlExportTests(ScriptFixture):
    def setUp(self):
        super().setUp()
        with admin_connect(DB) as conn:
            conn.execute("CREATE TABLE customers (id serial PRIMARY KEY, name text, balance numeric(10,2));"
                         "INSERT INTO customers (name, balance) VALUES ('Ann', 10.50), ('O''Neil', NULL)")

    def export(self, sql, fmt):
        return self.client.post(reverse("sql_export", args=[self.server.pk]), {"sql": sql, "db": DB, "format": fmt})

    def test_a_query_can_be_exported_as_insert_statements_for_its_table(self):
        response = self.export("SELECT id, name, balance FROM customers ORDER BY id", "sql")
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/sql", response["Content-Type"])
        self.assertTrue(response["Content-Disposition"].endswith('.sql"'))
        text = response.content.decode()
        self.assertIn('INSERT INTO "public"."customers" ("id", "name", "balance") VALUES', text)
        self.assertIn("('1', 'Ann', '10.50')", text)           # a query result has no column types, so every value is quoted and PostgreSQL converts it on INSERT
        self.assertIn("'O''Neil'", text)
        self.assertIn("NULL", text)

    def test_the_target_table_is_found_in_a_simple_select_and_a_placeholder_is_used_otherwise(self):
        from .views import _export_table
        self.assertEqual(_export_table("SELECT * FROM customers"), ("public", "customers"))
        self.assertEqual(_export_table('select id from "Sales"."Order Lines" o where id > 1 order by 1 limit 5;'), ("Sales", "Order Lines"))
        self.assertEqual(_export_table("SELECT * FROM audit.log WHERE note = 'FROM x, y'"), ("audit", "log"))
        for sql in ("SELECT * FROM a JOIN b ON a.id = b.id", "SELECT * FROM a, b", "SELECT 1", "SELECT * FROM generate_series(1, 3)",
                    "SELECT * FROM a UNION SELECT * FROM b", "SELECT * FROM (SELECT 1) s", "WITH x AS (SELECT 1) SELECT * FROM x"):
            with self.subTest(sql=sql):
                self.assertEqual(_export_table(sql), ("public", "query_result"))

    def test_csv_and_json_still_work_and_the_page_offers_all_three(self):
        self.assertIn("id,name,balance", self.export("SELECT id, name, balance FROM customers", "csv").content.decode())
        self.assertIn('"name": "Ann"', self.export("SELECT id, name FROM customers", "json").content.decode())
        script = (BASE_DIR / "static" / "sql_run.js").read_text(encoding="utf-8")
        self.assertIn('["sql", "SQL (INSERT statements)"]', script)


class SqlAjaxTests(ScriptFixture):
    def run_sql(self, sql, **extra):
        return self.client.post(reverse("sql_run", args=[self.server.pk]), json.dumps({"sql": sql, "db": DB, **extra}), content_type="application/json")

    def page(self, token, page, sql="", client=None):
        return (client or self.client).post(reverse("sql_page", args=[self.server.pk]), json.dumps({"token": token, "page": page, "sql": sql, "db": DB}), content_type="application/json")

    def test_a_result_comes_ten_rows_at_a_time_and_the_other_pages_by_ajax(self):
        data = self.run_sql("SELECT g AS n FROM generate_series(1, 25) g ORDER BY 1").json()
        self.assertTrue(data["ok"])
        self.assertEqual((data["total"], data["pages"], data["page"], data["first"], data["last"]), (25, 3, 1, 1, 10))
        self.assertEqual([r[0] for r in data["rows"]], [str(n) for n in range(1, 11)])
        last = self.page(data["token"], 3).json()
        self.assertEqual(([r[0] for r in last["rows"]], last["first"], last["last"]), (["21", "22", "23", "24", "25"], 21, 25))
        self.assertEqual(self.page(data["token"], 99).json()["page"], 3)                 # a page past the end is the last one
        self.assertEqual(self.page(data["token"], 0).json()["page"], 1)

    def test_a_forgotten_result_is_run_again_for_the_page_asked_for(self):
        sql = "SELECT g FROM generate_series(1, 25) g ORDER BY 1"
        again = self.page("gone", 2, sql=sql).json()
        self.assertEqual((again["ok"], again["page"], again["rows"][0][0]), (True, 2, "11"))
        self.assertFalse(self.page("gone", 2).json()["ok"])                              # nothing to run again

    def test_the_cap_still_applies_and_is_reported(self):
        data = self.run_sql("SELECT g FROM generate_series(1, 1500) g").json()
        self.assertEqual((data["total"], data["truncated"], data["pages"]), (1000, True, 100))

    def test_errors_and_statements_that_return_nothing_are_answered_in_json(self):
        bad = self.run_sql("SELECT nope FROM nowhere")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("nowhere", bad.json()["error"])
        self.assertEqual(self.run_sql("   ").status_code, 400)
        refused = self.run_sql("DELETE FROM customers")
        self.assertIn(refused.status_code, (400, 403))
        self.assertFalse(refused.json()["ok"])
        from audit.models import AuditEntry
        self.assertTrue(AuditEntry.objects.filter(action="sql.run", ok=False).exists())            # refused runs are in the audit log too

    def test_somebody_elses_kept_result_is_not_handed_over(self):
        from django.core.cache import cache
        secret = {"columns": ["x"], "rows": [["secret"]], "truncated": False, "count": 1, "message": "", "ms": 1}
        cache.set("pgdas-sqlres:theirs", {"user": self.staff.pk + 1000, "server": self.server.pk, "db": DB, "result": secret}, 60)
        answer = self.page("theirs", 1).json()
        self.assertFalse(answer["ok"])
        self.assertNotIn("secret", json.dumps(answer))
        cache.set("pgdas-sqlres:mine", {"user": self.staff.pk, "server": self.server.pk, "db": DB, "result": secret}, 60)
        self.assertEqual(self.page("mine", 1).json()["rows"], [["secret"]])
