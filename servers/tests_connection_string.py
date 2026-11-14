"""The connection-string parser of the Add server popup (static/connection_string.js), run with Node."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from access_control.models import Installation, Membership, Organization

SCRIPT = Path(settings.BASE_DIR) / "static" / "connection_string.js"


@unittest.skipUnless(shutil.which("node"), "needs Node.js to run the browser script")
class ConnectionStringTests(SimpleTestCase):
    def parse(self, text):
        code = f"const parse = require({json.dumps(str(SCRIPT))}); process.stdout.write(JSON.stringify(parse({json.dumps(text)})));"
        done = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_a_url_gives_every_field_and_says_what_it_left_out(self):
        result = self.parse("postgresql://alice:s3cret@db.example.com:5433/app?sslmode=require")
        self.assertEqual(result["values"], {"host": "db.example.com", "port": "5433", "username": "alice", "password": "s3cret", "sslmode": "require"})
        self.assertTrue(any("database name app" in note for note in result["ignored"]))

    def test_percent_encoded_and_raw_special_characters_in_the_password(self):
        self.assertEqual(self.parse("postgres://bob:p%40ss%23w%3Ard@h/db")["values"]["password"], "p@ss#w:rd")
        raw = self.parse("postgresql://bob:p@ss@host/db")["values"]
        self.assertEqual((raw["password"], raw["host"]), ("p@ss", "host"))
        self.assertEqual(self.parse("postgresql://u:Sprihan#2012@h/db")["values"]["password"], "Sprihan#2012")

    def test_no_port_leaves_the_default_alone(self):
        self.assertNotIn("port", self.parse("postgresql://u:p@h/db")["values"])

    def test_edge_cases_user_only_a_fragment_and_an_at_sign_in_the_query(self):
        self.assertEqual(self.parse("postgresql://alice@host/db")["values"], {"host": "host", "username": "alice"})
        self.assertEqual(self.parse("postgresql://u:p@h/db#frag")["values"], {"host": "h", "username": "u", "password": "p"})
        values = self.parse("postgresql://alice@host/db?application_name=a@b")["values"]
        self.assertEqual((values["host"], values["username"]), ("host", "alice"))
        self.assertEqual(self.parse("postgresql://host:6543/db")["values"], {"host": "host", "port": "6543"})

    def test_an_ipv6_address(self):
        values = self.parse("postgresql://u:p@[2001:db8::1]:5432/d")["values"]
        self.assertEqual((values["host"], values["port"]), ("2001:db8::1", "5432"))

    def test_the_keyword_form_with_quotes(self):
        values = self.parse("host=db.example.com port=5432 user=me password='a b \\'c\\'' sslmode=verify-full dbname=x")["values"]
        self.assertEqual(values, {"host": "db.example.com", "port": "5432", "username": "me", "password": "a b 'c'", "sslmode": "verify-full"})

    def test_a_jdbc_url_and_user_and_password_in_the_query(self):
        self.assertEqual(self.parse("jdbc:postgresql://h:5432/db?user=u&password=p&sslmode=disable")["values"],
                         {"host": "h", "port": "5432", "username": "u", "password": "p", "sslmode": "disable"})
        self.assertEqual(self.parse("postgresql://h/db?user=u&password=p+q")["values"]["password"], "p q")

    def test_several_hosts_use_the_first_and_say_so(self):
        result = self.parse("postgresql://u:p@h1:5432,h2:5433/db")
        self.assertEqual((result["values"]["host"], result["values"]["port"]), ("h1", "5432"))
        self.assertIn("the other hosts in the list", result["ignored"])

    def test_an_unknown_ssl_mode_and_other_options_are_left_out_not_guessed(self):
        result = self.parse("postgresql://u:p@h/db?sslmode=bogus&connect_timeout=5")
        self.assertNotIn("sslmode", result["values"])
        self.assertTrue(any("sslmode=bogus" in note for note in result["ignored"]))
        self.assertIn("connect_timeout", result["ignored"])

    def test_things_that_are_not_postgresql_connection_strings(self):
        for text in ("", "   ", "hello world", "mysql://u:p@h/db"):
            with self.subTest(text=text):
                self.assertIn("error", self.parse(text))


class PopupHasTheBoxTests(TestCase):
    def test_the_box_and_the_script_are_on_the_page_for_super_users(self):
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        org = Organization.objects.create(name="Org")
        admin = get_user_model().objects.create_user("a", email="a@o.test", password="pw-12345-abc", is_staff=True, is_superuser=True)
        Membership.objects.create(user=admin, organization=org, role="super_user")
        client = Client()
        client.force_login(admin)
        page = client.get(reverse("server_list"))
        for needle in ("data-connection-string", "data-connection-fill", "connection_string.js"):
            self.assertContains(page, needle)