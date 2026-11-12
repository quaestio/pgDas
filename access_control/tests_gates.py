"""The two gates: PGDAS (the employee's access) first, PostgreSQL (the login saved on the server) second. Each says which one stopped you."""
import psycopg
from django.test import SimpleTestCase

from explorer import pg


class SecondGateMessageTests(SimpleTestCase):
    def setUp(self):
        self.token = pg._login.set("report_ro")

    def tearDown(self):
        pg._login.reset(self.token)

    def test_a_missing_privilege_names_the_login_and_says_pgdas_allowed_it(self):
        text = pg._message(psycopg.errors.InsufficientPrivilege("permission denied for table orders"))
        self.assertIn("PostgreSQL blocked this", text)
        self.assertIn('"report_ro"', text)
        self.assertIn("permission denied for table orders", text)
        self.assertIn("PGDAS allowed it", text)
        self.assertIn("Ask a Super User", text)

    def test_a_read_only_login_says_so(self):
        text = pg._message(psycopg.errors.ReadOnlySqlTransaction("cannot execute INSERT in a read-only transaction"))
        self.assertIn("PostgreSQL blocked this", text)
        self.assertIn("is read-only", text)
        self.assertIn('"report_ro"', text)

    def test_other_errors_are_unchanged(self):
        self.assertEqual(pg._message(psycopg.errors.UndefinedTable('relation "x" does not exist')), 'relation "x" does not exist')