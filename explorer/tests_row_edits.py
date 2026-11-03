"""Editing rows in the data grid: one reviewed batch (change cells, add rows, delete rows), recorded, and undoable from the audit log."""
import json
import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from audit.models import AuditEntry

from .tests import admin_connect
from .tests_admin import AdminFixture, DB

BASE_DIR = Path(__file__).resolve().parent.parent


class GridFixture(AdminFixture, TestCase):
    def setUp(self):
        super().setUp()
        with admin_connect("postgres") as admin:
            admin.execute(f"CREATE DATABASE {DB}")
        with admin_connect(DB) as conn:
            conn.execute("CREATE TABLE people (id serial PRIMARY KEY, name text NOT NULL, email text, age int, total int GENERATED ALWAYS AS (coalesce(age, 0) * 2) STORED);"
                         "INSERT INTO people (name, email, age) VALUES ('Ann', 'a@x', 30), ('Bob', 'b@x', 41), ('Cy', NULL, NULL);"
                         "CREATE TABLE loose (a int, b text); INSERT INTO loose VALUES (1, 'x')")
        self.client = Client()
        self.client.force_login(self.staff)

    def rows(self, query="SELECT id, name, email, age, total FROM people ORDER BY id"):
        with admin_connect(DB) as conn:
            return conn.execute(query).fetchall()

    def send(self, name, body, client=None):
        return (client or self.client).post(reverse(name, args=[self.server.pk]), json.dumps({"db": DB, **body}), content_type="application/json")

    def batch(self, **parts):
        return {"schema": "public", "table": "people", "edits": [], "inserts": [], "deletes": [], **parts}

    def save(self, batch, confirm=None):
        preview = self.send("rows_preview", batch)
        self.assertEqual(preview.status_code, 200, preview.content)
        data = preview.json()
        return data, self.send("rows_execute", {**batch, "fingerprint": data["fingerprint"], "confirm": confirm})


class SaveTests(GridFixture):
    def test_edits_inserts_and_deletes_are_previewed_as_one_list_and_nothing_changes_before_saving(self):
        batch = self.batch(edits=[{"key": {"id": "1"}, "old": {"age": "30"}, "set": {"age": "31", "email": None}}],
                           inserts=[{"name": "Di", "age": "5"}], deletes=[{"key": {"id": "3"}}])
        data = self.send("rows_preview", batch).json()
        self.assertEqual((data["count"], data["title"], data["confirm_word"], data["button"]), (3, "SAVE 3 CHANGES?", "ZAP", "Save 3 changes"))
        texts = [o["text"] for o in data["objects"]]
        self.assertEqual(texts, ["~ change row id=1: age: '30' → '31', email: 'a@x' → NULL", "+ add row name='Di', age='5'", "− delete row id=3"])
        self.assertIn("UPDATE", data["statement"])
        self.assertEqual(self.rows()[0], (1, "Ann", "a@x", 30, 60))                      # the preview was rolled back
        self.assertEqual(len(self.rows()), 3)

    def test_saving_applies_everything_records_one_entry_and_undo_puts_it_all_back(self):
        before = self.rows()
        batch = self.batch(edits=[{"key": {"id": "1"}, "old": {"age": "30"}, "set": {"age": "31", "email": None}}],
                           inserts=[{"name": "Di", "age": "5"}], deletes=[{"key": {"id": "3"}}])
        _, response = self.save(batch, confirm="ZAP")
        self.assertEqual(response.status_code, 200, response.content)
        after = self.rows()
        self.assertEqual(after[:2], [(1, "Ann", None, 31, 62), (2, "Bob", "b@x", 41, 82)])
        self.assertEqual((len(after), after[2][1:]), (3, ("Di", None, 5, 10)))       # (its id may skip a number: the preview used one, sequences never roll back)
        entry = AuditEntry.objects.get(action="rows.edit")
        self.assertEqual((entry.ok, entry.rows, entry.payload["kind"], entry.payload["pk"]), (True, 3, "rows", ["id"]))
        self.assertTrue(entry.can_undo)
        self.assertEqual(entry.record_label, "grid edit · 3 rows in public.people")
        undo = self.client.post(reverse("audit_undo", args=[entry.pk]))
        self.assertEqual(undo.status_code, 302)
        self.assertEqual(self.rows(), before)                                           # changed, added and deleted rows are all back as they were
        entry.refresh_from_db()
        self.assertIsNotNone(entry.restored_at)
        self.assertFalse(entry.can_undo)
        again = self.client.post(reverse("audit_undo", args=[entry.pk]), follow=True)
        self.assertContains(again, "already undone")

    def test_a_delete_needs_the_word_and_edits_alone_do_not(self):
        data, response = self.save(self.batch(deletes=[{"key": {"id": "2"}}]), confirm="zap")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Type ZAP", response.json()["error"])
        self.assertEqual(len(self.rows()), 3)
        data, response = self.save(self.batch(edits=[{"key": {"id": "2"}, "old": {"name": "Bob"}, "set": {"name": "Rob"}}]))
        self.assertIsNone(data["confirm_word"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.rows()[1][1], "Rob")

    def test_a_cell_changed_by_someone_else_stops_the_whole_save(self):
        batch = self.batch(edits=[{"key": {"id": "1"}, "old": {"age": "30"}, "set": {"age": "99"}}], inserts=[{"name": "Zed"}])
        with admin_connect(DB) as conn:
            conn.execute("UPDATE people SET age = 32 WHERE id = 1")
        response = self.send("rows_preview", batch)
        self.assertEqual(response.status_code, 400)
        self.assertIn("was changed by someone else", response.json()["error"])
        self.assertEqual(len(self.rows()), 3)

    def test_a_save_stops_if_the_rows_changed_between_preview_and_save(self):
        batch = self.batch(deletes=[{"key": {"id": "3"}}], edits=[{"key": {"id": "1"}, "old": {"name": "Ann"}, "set": {"name": "Anna"}}])
        preview = self.send("rows_preview", batch).json()
        with admin_connect(DB) as conn:
            conn.execute("UPDATE people SET email = 'new@x' WHERE id = 3")           # a row that is about to be deleted changed
        response = self.send("rows_execute", {**batch, "fingerprint": preview["fingerprint"], "confirm": "ZAP"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("changed since you checked", response.json()["error"])
        self.assertEqual(len(self.rows()), 3)

    def test_a_failing_row_undoes_the_rest_of_the_batch(self):
        batch = self.batch(edits=[{"key": {"id": "1"}, "old": {"name": "Ann"}, "set": {"name": "Anna"}}], inserts=[{"age": "5"}])    # name is NOT NULL
        response = self.send("rows_preview", batch)
        self.assertEqual(response.status_code, 400)
        self.assertIn("null value", response.json()["error"])
        self.assertEqual(self.rows()[0][1], "Ann")

    def test_undo_is_refused_when_the_rows_changed_again(self):
        _, response = self.save(self.batch(edits=[{"key": {"id": "2"}, "old": {"age": "41"}, "set": {"age": "42"}}]))
        self.assertEqual(response.status_code, 200)
        with admin_connect(DB) as conn:
            conn.execute("UPDATE people SET age = 50 WHERE id = 2")
        entry = AuditEntry.objects.get(action="rows.edit")
        followed = self.client.post(reverse("audit_undo", args=[entry.pk]), follow=True)
        self.assertContains(followed, "changed or deleted again")
        self.assertEqual(self.rows()[1][3], 50)


class RefusalTests(GridFixture):
    def test_generated_and_key_columns_and_missing_keys_are_refused(self):
        cases = ((self.batch(edits=[{"key": {"id": "1"}, "set": {"total": "1"}}]), "generated by the database"),
                 (self.batch(edits=[{"key": {"id": "1"}, "set": {"id": "9"}}]), "primary key"),
                 (self.batch(edits=[{"key": {"name": "Ann"}, "set": {"age": "1"}}]), "without its primary key"),
                 (self.batch(inserts=[{"total": "1"}]), "generated by the database"),
                 (self.batch(edits=[{"key": {"id": "1"}, "set": {"nope": "1"}}]), "does not exist"),
                 (self.batch(), "Nothing has been changed"),
                 (self.batch(table="loose", inserts=[{"a": "2"}]), "no primary key"))
        for batch, message in cases:
            with self.subTest(message=message):
                response = self.send("rows_preview", batch)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json()["error"])

    def test_only_staff_may_save(self):
        plain = Client()
        plain.force_login(get_user_model().objects.create_user("pat", password="pw-12345-abc"))
        for name in ("rows_preview", "rows_execute"):
            self.assertEqual(self.send(name, self.batch(inserts=[{"name": "x"}]), client=plain).status_code, 403)
        self.assertEqual(len(self.rows()), 3)


class GridPageTests(GridFixture):
    def page(self, client=None, table="people"):
        return (client or self.client).get(reverse("detail"), {"type": "table", "server": self.server.pk, "db": DB, "schema": "public", "table": table, "tab": "data"}).content.decode()

    def test_staff_get_an_editable_grid_with_the_exact_text_of_every_cell(self):
        html = self.page()
        meta = json.loads(re.search(r'data-grid="([^"]*)"', html).group(1).replace("&quot;", '"'))
        self.assertEqual((meta["schema"], meta["table"], meta["pk"]), ("public", "people", ["id"]))
        by_name = {c["name"]: c for c in meta["columns"]}
        self.assertTrue(by_name["total"]["locked"] and by_name["id"]["key"] and by_name["id"]["default"])
        self.assertEqual(len(re.findall(r"<tr data-row-key=", html)), 3)
        self.assertIn('data-raw="a@x"', html)
        self.assertIn('data-null="1"', html)                                              # Cy has no email
        for needle in ("data-grid-add", "data-grid-save", "data-grid-discard", "data-grid-pick"):
            self.assertIn(needle, html)

    def test_a_table_without_a_primary_key_says_so_and_has_no_grid(self):
        html = self.page(table="loose")
        self.assertIn("has no primary key", html)
        self.assertNotIn("data-grid=", html)

    def test_people_who_are_not_staff_get_the_plain_table(self):
        plain = Client()
        plain.force_login(get_user_model().objects.create_user("pat", password="pw-12345-abc"))
        html = self.page(plain)
        self.assertNotIn("data-grid", html)
        self.assertIn("Ann", html)

    def test_scripts_are_wired_and_safe(self):
        home = self.client.get("/").content.decode()
        self.assertIn('src="/static/row_edit.js"', home)
        self.assertIn('data-rows-preview-url="/c/0/rows/preview/"', home)
        script = (BASE_DIR / "static" / "row_edit.js").read_text(encoding="utf-8")
        for needle in ("pgdasReview.start", "rows: payload(grid)", "Save or discard your changes first", "data-new-null", "tr.deleting"):
            self.assertIn(needle, script)
        self.assertNotIn("innerHTML", script)
        self.assertNotRegex(script, r"\bconfirm\(")
        review = (BASE_DIR / "static" / "review.js").read_text(encoding="utf-8")
        self.assertIn("options.rows", review)
