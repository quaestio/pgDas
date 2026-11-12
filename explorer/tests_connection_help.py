"""Plain-language messages when a remote PostgreSQL refuses or cannot be reached."""
import psycopg
from django.test import SimpleTestCase, override_settings

from explorer import pg
from explorer.connection_help import friendly


def operational(text):
    return psycopg.OperationalError(text)


class ConnectionHelpTests(SimpleTestCase):
    def setUp(self):
        self.token = pg._target.set(("db.example.com", 5432, "report", "sales"))

    def tearDown(self):
        pg._target.reset(self.token)

    def test_an_address_not_allowed_by_postgresql_says_which_address_and_gives_the_pg_hba_line(self):
        text = pg._message(operational('connection failed: connection to server at "198.51.100.9", port 5432 failed: FATAL:  '
                                       'no pg_hba.conf entry for host "203.0.113.7", user "report", database "sales", no encryption'))
        self.assertIn("does not allow this PGDAS server (203.0.113.7)", text)
        self.assertIn("host  sales  report  203.0.113.7/32  scram-sha-256", text)
        self.assertIn("pg_hba.conf", text)
        self.assertIn("systemctl reload postgresql", text)
        self.assertIn("listen_addresses", text)
        self.assertIn("firewall", text)
        self.assertIn("not even checked", text)

    def test_a_timeout_or_refusal_says_to_allow_pgdas_through_the_firewall_and_uses_the_configured_address(self):
        with override_settings(PUBLIC_IP="203.0.113.7"):
            for reason in ("timeout expired", "connection to server at \"1.2.3.4\", port 5432 failed: Connection refused",
                           "connection to server failed: No route to host"):
                with self.subTest(reason=reason):
                    text = pg._message(operational(reason))
                    self.assertIn("could not reach db.example.com on port 5432", text)
                    self.assertIn("203.0.113.7", text)
                    self.assertIn("listen_addresses", text)

    def test_without_a_known_address_it_tells_them_to_ask_for_it(self):
        with override_settings(PUBLIC_IP=""):
            self.assertIn("ask the person who runs PGDAS for its public IP address", pg._message(operational("timeout expired")))

    def test_a_wrong_password_is_worded_plainly_and_names_the_login(self):
        text = pg._message(operational('connection failed: FATAL:  password authentication failed for user "report"'))
        self.assertIn("did not accept the user name or password", text)
        self.assertIn("'report'", text)
        self.assertNotIn("pg_hba", text)

    def test_an_unknown_host_name(self):
        self.assertIn("was not found", pg._message(operational('could not translate host name "nope.invalid" to address: Name or service not known')))

    def test_other_errors_are_left_alone(self):
        self.assertIsNone(friendly('relation "x" does not exist'))
        self.assertEqual(pg._message(psycopg.errors.UndefinedTable('relation "x" does not exist')), 'relation "x" does not exist')