"""Managing the partitions of an existing partitioned table: list them, add a new one, attach a table, detach one."""
from pathlib import Path

from django.urls import reverse

from . import pg
from .tests import admin_connect
from .tests_admin import DB
from .tests_table_script import ScriptFixture

BASE_DIR = Path(__file__).resolve().parent.parent


class ManagePartitionTests(ScriptFixture):
    def setUp(self):
        super().setUp()
        with admin_connect(DB) as conn:
            conn.execute("CREATE TABLE sales (id bigint, saledate date NOT NULL) PARTITION BY RANGE (saledate);"
                         "CREATE TABLE sales_2024 PARTITION OF sales FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');"
                         "CREATE TABLE sales_rest PARTITION OF sales DEFAULT")

    def parts(self):
        return sorted(r[0] for r in self.query("SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = 'sales'::regclass"))

    def detail(self, tab, table="sales", type="table"):
        return self.client.get(reverse("detail"), {"type": type, "server": self.server.pk, "db": DB, "schema": "public", "table": table, "tab": tab})

    def test_a_new_partition_is_created_with_one_statement(self):
        _, response = self.go('CREATE TABLE "public"."sales_2025" PARTITION OF "public"."sales" FOR VALUES FROM (\'2025-01-01\') TO (\'2026-01-01\')')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.parts(), ["sales_2024", "sales_2025", "sales_rest"])

    def test_an_existing_table_is_attached_and_its_rows_are_checked(self):
        with admin_connect(DB) as conn:
            conn.execute("CREATE TABLE old (id bigint, saledate date NOT NULL); INSERT INTO old VALUES (1, '2023-05-05')")
        _, response = self.go('ALTER TABLE "public"."sales" ATTACH PARTITION "public"."old" FOR VALUES FROM (\'2023-01-01\') TO (\'2024-01-01\')')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.parts(), ["old", "sales_2024", "sales_rest"])
        with admin_connect(DB) as conn:
            conn.execute("CREATE TABLE wrong (id bigint, saledate date NOT NULL); INSERT INTO wrong VALUES (1, '1990-01-01')")
        preview = self.send("write_preview", 'ALTER TABLE "public"."sales" ATTACH PARTITION "public"."wrong" FOR VALUES FROM (\'2022-01-01\') TO (\'2023-01-01\')')
        self.assertEqual(preview.status_code, 400)
        self.assertIn("violate", preview.json()["error"])
        self.assertNotIn("wrong", self.parts())

    def test_detaching_asks_for_the_word_alter_and_keeps_the_rows(self):
        with admin_connect(DB) as conn:
            conn.execute("INSERT INTO sales VALUES (1, '2024-03-03')")
        sql = 'ALTER TABLE "public"."sales" DETACH PARTITION "public"."sales_2024"'
        data, refused = self.go(sql)
        self.assertEqual(data["confirm_word"], "ALTER")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("sales_2024", self.parts())
        _, done = self.go(sql, confirm="ALTER")
        self.assertEqual(done.status_code, 200, done.content)
        self.assertEqual(self.parts(), ["sales_rest"])
        self.assertEqual(self.query("SELECT count(*) FROM sales_2024"), [(1,)])           # an ordinary table now, rows kept
        self.assertEqual(self.query("SELECT relispartition FROM pg_class WHERE oid = 'sales_2024'::regclass"), [(False,)])

    def test_the_partitions_are_described_for_the_tab_and_the_tree(self):
        with pg.connect(self.server, DB) as conn:
            info = pg.relation_info(conn, "public", "sales")
            self.assertEqual((info["strategy"], info["partition_key"]), ("range", "RANGE (saledate)"))
            child = pg.relation_info(conn, "public", "sales_2024")
            self.assertEqual(child["parent"], {"schema": "public", "name": "sales"})
            self.assertIn("FROM ('2024-01-01') TO ('2025-01-01')", child["bound"])
            partitions = pg.table_partitions(conn, "public", "sales")
        self.assertEqual([(p["name"], p["default"]) for p in partitions], [("sales_2024", False), ("sales_rest", True)])
        html = self.detail("partitions").content.decode()
        for text in ("sales_2024", "default", "RANGE (saledate)", 'data-table-dialog="add-partition"', 'data-table-dialog="attach-partition"', "data-partition-detach="):
            self.assertIn(text, html)
        self.assertIn('data-partitions-tab="', html)
        folder = self.client.get(reverse("tree"), {"type": "table", "server": self.server.pk, "db": DB, "schema": "public", "table": "sales"}).json()["nodes"]
        self.assertEqual(folder[0]["label"], "Partitions")
        nodes = self.client.get(reverse("tree"), {"type": "partitions", "server": self.server.pk, "db": DB, "schema": "public", "table": "sales"}).json()["nodes"]
        self.assertEqual([n["label"] for n in nodes], ["sales_2024", "sales_rest"])
        self.assertTrue(all(n["type"] == "table" for n in nodes))
        plain = self.client.get(reverse("tree"), {"type": "table", "server": self.server.pk, "db": DB, "schema": "public", "table": "sales_2024"}).json()["nodes"]
        self.assertNotIn("Partitions", [n["label"] for n in plain])

    def test_a_partition_shows_its_parent_and_a_detach_button(self):
        html = self.detail("properties", table="sales_2024").content.decode()
        self.assertIn("Partition of", html)
        self.assertIn("data-partition-detach-from=\"public.sales\"", html)
        parent = self.detail("properties").content.decode()
        self.assertIn("Partitioned by", parent)
        self.assertNotIn("Detach from", parent)

    def test_columns_of_a_partitioned_table_offer_no_reordering(self):
        html = self.detail("columns").content.decode()
        self.assertIn('data-table-dialog="add-column"', html)
        self.assertNotIn("data-col-move", html)
        self.assertIn("data-col-move", self.detail("columns", table="sales_2024").content.decode())

    def test_the_dialogs_build_the_statements(self):
        script = (BASE_DIR / "static" / "table_dialogs.js").read_text(encoding="utf-8")
        for needle in ('"add-partition"', '"attach-partition"', " PARTITION OF ", " ATTACH PARTITION ", " DETACH PARTITION ", "boundFields", "boundProblem", "boundSql",
                       "data-partition-detach", "data-partitions-tab", "choose a table…"):
            self.assertIn(needle, script)
        self.assertNotIn("innerHTML", script)
