"""Server numbers in links and requests are encrypted tokens (servers/ids.py)."""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from access_control.models import Installation, Membership, Organization, OrganizationServer
from servers import ids
from servers.models import ServerConnection
from watch.models import ServerHealth


class TokenTests(SimpleTestCase):
    def test_a_number_goes_to_a_token_and_back(self):
        for number in (1, 2, 3, 99, 12345, 2 ** 31, 2 ** 32 + 7, 2 ** 63 - 1):
            token = ids.encode(number)
            self.assertEqual(len(token), 19, number)
            self.assertFalse(token.isdigit())
            self.assertEqual(ids.decode(token), number)

    def test_the_same_server_always_gets_the_same_token_and_different_servers_different_ones(self):
        self.assertEqual(ids.encode(5), ids.encode(5))
        self.assertEqual(len({ids.encode(n) for n in range(1, 500)}), 499)
        self.assertNotIn("5", ids.encode(5)[:0])
        self.assertNotEqual(ids.encode(2)[:6], ids.encode(3)[:6])                   # neighbouring numbers do not look alike

    def test_a_changed_or_invented_token_is_refused(self):
        token = ids.encode(7)
        for bad in (token[:-1] + ("A" if token[-1] != "A" else "B"), "A" + token[1:] if token[0] != "A" else "B" + token[1:], token[:-2], token + "x", "not-a-token-at-all!!", "?", "../../etc"):
            with self.subTest(bad=bad):
                self.assertIsNone(ids.decode(bad))
        self.assertIsNone(ids.decode(""))
        self.assertIsNone(ids.decode(None))

    def test_the_key_comes_from_the_installations_secret(self):
        token = ids.encode(7)
        with override_settings(SECRET_KEY="another-secret-key", ENCRYPTION_KEY=""):
            self.assertNotEqual(ids.encode(7), token)
            self.assertIsNone(ids.decode(token))

    def test_plain_numbers_still_work_until_they_are_switched_off(self):
        self.assertEqual(ids.decode("12"), 12)
        self.assertEqual(ids.to_digits(ids.encode(12)), "12")
        self.assertEqual(ids.to_token("12"), ids.encode(12))
        self.assertEqual(ids.to_digits("garbage"), "garbage")                        # a bad value stays a bad value
        with override_settings(PGDAS_STRICT_SERVER_IDS=True):
            self.assertIsNone(ids.decode("12"))
            self.assertEqual(ids.decode(ids.encode(12)), 12)
            self.assertEqual(ids.decode("0"), 0)                                      # the placeholder pages fill in later

    def test_zero_stays_zero_for_the_address_templates_the_pages_fill_in(self):
        self.assertEqual(ids.encode(0), "0")
        self.assertEqual(reverse("write_preview", args=[0]), "/c/0/sql/write/preview/")

    def test_an_address_can_be_made_from_the_number_or_from_the_token(self):
        self.assertEqual(reverse("sql_runner", args=[7]), reverse("sql_runner", args=[ids.encode(7)]))
        self.assertEqual(reverse("sql_runner", args=["7"]), reverse("sql_runner", args=[ids.encode(7)]))
        from django.urls import NoReverseMatch
        with self.assertRaises(NoReverseMatch):
            reverse("sql_runner", args=["not-a-token"])

    def test_addresses_carry_the_token_not_the_number(self):
        path = reverse("sql_runner", args=[7])
        self.assertEqual(path, f"/c/{ids.encode(7)}/sql/")
        self.assertNotIn("/c/7/", path)


class RequestTests(TestCase):
    def setUp(self):
        cache.clear()
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Org")
        self.admin = get_user_model().objects.create_user("a", email="a@o.test", password="pw-12345-abc", is_staff=True, is_superuser=True, email_verified=True)
        Membership.objects.create(user=self.admin, organization=self.org, role="super_user")
        self.server = ServerConnection.objects.create(name="Mine", host="h", username="u")
        OrganizationServer.objects.create(server=self.server, organization=self.org)
        ServerHealth.objects.create(server=self.server, version_num=180000, version_text="PostgreSQL 18.0", sample_count=1)
        self.client = Client()
        self.client.force_login(self.admin)

    def test_the_dashboards_page_and_data_take_the_token_and_hand_out_tokens(self):
        token = self.server.sid
        page = self.client.get(reverse("dashboards"), {"server": token})
        self.assertContains(page, f'<option value="{token}" selected>')
        self.assertNotContains(page, f'<option value="{self.server.pk}"')
        data = self.client.get(reverse("dashboards_data"), {"server": token}, headers={"X-Requested-With": "fetch"}).json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["server"]["id"], token)

    def test_an_old_plain_number_link_still_opens_it(self):
        self.assertEqual(self.client.get(reverse("dashboards_data"), {"server": self.server.pk}).status_code, 200)

    def test_a_made_up_token_gets_the_plain_server_not_found_answer(self):
        response = self.client.get(reverse("dashboards"), {"server": "AAAAAAAAAAAAAAAAAAA"})
        self.assertContains(response, "was not found", status_code=404)

    def test_with_plain_numbers_switched_off_only_the_token_works(self):
        with override_settings(PGDAS_STRICT_SERVER_IDS=True):
            self.assertEqual(self.client.get(reverse("dashboards_data"), {"server": self.server.pk}).status_code, 404)
            self.assertEqual(self.client.get(reverse("dashboards_data"), {"server": self.server.sid}).status_code, 200)

    def test_the_tree_and_the_overview_hand_out_tokens_only(self):
        nodes = self.client.get(reverse("tree"), {"type": "servers"}).json()["nodes"]
        self.assertEqual([n["server"] for n in nodes], [self.server.sid])
        html = self.client.get(reverse("detail"), {"type": "servers"}).content.decode()
        self.assertIn(f'data-open-server="{self.server.sid}"', html)
        self.assertNotIn(f'data-open-server="{self.server.pk}"', html)
        self.assertIn(reverse("sql_runner", args=[self.server.pk]), html)

    def test_a_setting_request_sent_by_the_page_carries_the_token(self):
        import json
        response = self.client.post(reverse("setting_preview"), json.dumps({"server": self.server.sid, "name": "work_mem", "value": "8MB"}), content_type="application/json")
        self.assertNotEqual(response.status_code, 404)                                # the server is found (the preview may still fail: it is not a real one)
        gone = self.client.post(reverse("setting_preview"), json.dumps({"server": "AAAAAAAAAAAAAAAAAAA", "name": "work_mem", "value": "8MB"}), content_type="application/json")
        self.assertEqual(gone.status_code, 404)
