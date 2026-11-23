"""Alerts per organization: routing, confirmed recipients, stop links, safe webhooks, limits and the settings page."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from access_control.models import AlertRecipient, Installation, Membership, Organization, OrganizationServer
from audit.models import AuditEntry
from servers.models import ServerConnection

from . import notify, recipients
from .models import AlertState

MAIL = dict(EMAIL_HOST="smtp.test", EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
CRITICAL = [{"level": "critical", "title": "2 sessions are blocked by locks", "detail": "waiting", "key": "blocked"}]


class Fixture(TestCase):
    def setUp(self):
        cache.clear()
        mail.outbox = []
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Alpha")
        self.other = Organization.objects.create(name="Beta")
        self.admin = get_user_model().objects.create_user("a", email="a@alpha.test", password="pw-12345-abc", is_staff=True, is_superuser=True, email_verified=True)
        Membership.objects.create(user=self.admin, organization=self.org, role="super_user")
        self.server = self.make("Alpha DB", self.org)
        self.theirs = self.make("Beta DB", self.other)

    def make(self, name, org):
        server = ServerConnection.objects.create(name=name, host="h", username="u")
        OrganizationServer.objects.create(server=server, organization=org)
        return server

    def confirmed(self, org, email):
        return AlertRecipient.objects.create(organization=org, email=email, verified=True)


@override_settings(MODE="shared", ALERT_WEBHOOK_URL="", ALERT_EMAIL_TO=["operator@platform.test"], **MAIL)
class RoutingTests(Fixture):
    def test_on_a_shared_installation_an_organization_with_nothing_set_gets_nothing_and_the_operators_list_is_not_used(self):
        self.assertEqual(notify.channels_for(self.server)["source"], "none")
        self.assertIsNone(notify.run())
        self.assertFalse(AlertState.objects.exists())

    @override_settings(MODE="dedicated")
    def test_on_a_dedicated_installation_the_installations_list_is_the_fallback(self):
        chosen = notify.channels_for(self.server)
        self.assertEqual((chosen["source"], chosen["emails"]), ("installation", ["operator@platform.test"]))

    def test_an_organizations_own_channels_replace_the_fallback(self):
        self.confirmed(self.org, "ops@alpha.test")
        chosen = notify.channels_for(self.server)
        self.assertEqual((chosen["source"], chosen["emails"]), ("organization", ["ops@alpha.test"]))
        with override_settings(MODE="dedicated"):
            self.assertEqual(notify.channels_for(self.server)["emails"], ["ops@alpha.test"])                  # not the installation's

    def test_an_unconfirmed_address_receives_nothing_and_does_not_count(self):
        AlertRecipient.objects.create(organization=self.org, email="stranger@x.test", verified=False)
        self.assertEqual(notify.channels_for(self.server)["source"], "none")

    def test_alerts_go_only_to_the_organization_they_belong_to(self):
        self.confirmed(self.org, "ops@alpha.test")
        self.confirmed(self.other, "ops@beta.test")
        notify.check_server(self.server, current=CRITICAL)
        notify.check_server(self.theirs, current=CRITICAL)
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ["ops@alpha.test", "ops@beta.test"])
        for message in mail.outbox:
            self.assertIn("Alpha DB" if message.to[0].endswith("alpha.test") else "Beta DB", message.subject)
            self.assertNotIn("Beta DB" if message.to[0].endswith("alpha.test") else "Alpha DB", message.subject + message.body)

    def test_run_sends_only_for_servers_that_have_a_channel(self):
        self.confirmed(self.org, "ops@alpha.test")
        with mock.patch("watch.alerts.evaluate", return_value=CRITICAL):
            results = notify.run()
        self.assertEqual([r["server"] for r in results], ["Alpha DB"])
        self.assertFalse(AlertState.objects.filter(server=self.theirs).exists())

    def test_without_a_mail_host_confirmed_recipients_are_not_used(self):
        self.confirmed(self.org, "ops@alpha.test")
        with override_settings(EMAIL_HOST=""):
            self.assertEqual(notify.channels_for(self.server)["source"], "none")

    def test_each_recipient_gets_its_own_message_with_a_stop_link(self):
        self.confirmed(self.org, "one@alpha.test")
        self.confirmed(self.org, "two@alpha.test")
        notify.check_server(self.server, current=CRITICAL)
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ["one@alpha.test", "two@alpha.test"])
        for message in mail.outbox:
            self.assertIn("To stop:", message.body)
            self.assertIn("/watch/alerts/stop/", message.body)
            self.assertIn("Alpha", message.body)

    @override_settings(ALERT_MAX_PER_HOUR=2)
    def test_an_organization_cannot_send_more_than_the_hourly_limit(self):
        self.confirmed(self.org, "ops@alpha.test")
        results = []
        for number in range(4):
            results.append(notify.check_server(self.server, current=[{"level": "critical", "title": f"alert {number}", "detail": "", "key": f"k{number}"}]))
        self.assertEqual([bool(r["sent"]) for r in results], [True, True, False, False])
        self.assertEqual(results[2]["failed"][0][0], "limit")
        self.assertEqual(len(mail.outbox), 2)
        self.assertTrue(AuditEntry.objects.filter(action="alert.notify", ok=False, detail__contains="hourly limit").exists())


@override_settings(MODE="shared", **MAIL)
class RecipientTests(Fixture):
    def test_a_new_address_waits_for_its_owner_and_a_scanner_opening_the_link_changes_nothing(self):
        recipient, outcome = recipients.add_recipient(self.org, "New.Person@Example.test")
        self.assertEqual((outcome, recipient.verified, recipient.email), ("waiting", False, "new.person@example.test"))
        self.assertEqual(mail.outbox[-1].to, ["new.person@example.test"])
        link = recipients._link("alert_confirm", recipients.CONFIRM_SALT, recipient)
        path = link.replace(recipients.base_url(), "")
        client = Client()
        self.assertContains(client.get(path), "Confirm")
        recipient.refresh_from_db()
        self.assertFalse(recipient.verified)                                              # opening the link does nothing
        self.assertContains(client.post(path), "will now receive the alerts of Alpha")
        recipient.refresh_from_db()
        self.assertTrue(recipient.verified)

    def test_a_forged_or_expired_confirmation_link_is_refused(self):
        recipient, _ = recipients.add_recipient(self.org, "who@example.test")
        path = recipients._link("alert_confirm", recipients.CONFIRM_SALT, recipient).replace(recipients.base_url(), "")
        self.assertEqual(Client().get(path[:-3] + "x/").status_code, 400)
        with mock.patch("watch.recipients.CONFIRM_DAYS", 0):
            self.assertEqual(Client().get(path).status_code, 400)

    def test_an_address_of_a_verified_member_is_confirmed_at_once(self):
        recipient, outcome = recipients.add_recipient(self.org, "A@Alpha.test")
        self.assertEqual((outcome, recipient.verified), ("confirmed", True))
        self.assertEqual(mail.outbox, [])                                                   # nothing to confirm

    def test_a_member_of_another_organization_does_not_count(self):
        recipient, outcome = recipients.add_recipient(self.other, "a@alpha.test")
        self.assertEqual((outcome, recipient.verified), ("waiting", False))

    def test_duplicates_bad_addresses_and_the_limit(self):
        self.assertEqual(recipients.add_recipient(self.org, "x@example.test")[1], "waiting")
        self.assertEqual(recipients.add_recipient(self.org, "X@example.test")[1], "exists")
        with self.assertRaisesMessage(ValueError, "not an email address"):
            recipients.add_recipient(self.org, "nonsense")
        with override_settings(ALERT_MAX_RECIPIENTS=2):
            recipients.add_recipient(self.org, "y@example.test")
            with self.assertRaisesMessage(ValueError, "at most 2"):
                recipients.add_recipient(self.org, "z@example.test")

    def test_the_confirmation_email_is_not_sent_again_within_ten_minutes(self):
        recipient, _ = recipients.add_recipient(self.org, "who@example.test")
        self.assertFalse(recipients.send_confirmation(recipient))
        cache.clear()
        self.assertTrue(recipients.send_confirmation(recipient))

    def test_the_stop_link_removes_the_address_and_then_stops_working(self):
        recipient = self.confirmed(self.org, "ops@alpha.test")
        path = recipients.stop_link(recipient).replace(recipients.base_url(), "")
        client = Client()
        self.assertContains(client.get(path), "Stop sending alerts")
        self.assertTrue(AlertRecipient.objects.filter(pk=recipient.pk).exists())          # showing the page removes nothing
        self.assertContains(client.post(path), "will no longer receive")
        self.assertFalse(AlertRecipient.objects.filter(pk=recipient.pk).exists())
        self.assertEqual(client.get(path).status_code, 400)

    def test_a_link_for_one_address_cannot_be_used_for_another(self):
        recipient = self.confirmed(self.org, "ops@alpha.test")
        path = recipients.stop_link(recipient).replace(recipients.base_url(), "")
        AlertRecipient.objects.filter(pk=recipient.pk).update(email="changed@alpha.test")
        self.assertEqual(Client().get(path).status_code, 400)


class Receiver(HTTPServer):
    allow_reuse_address = True


def handler(status, location=None, seen=None):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if seen is not None:
                seen.append(self.rfile.read(length).decode())
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.end_headers()

        def log_message(self, *args):
            pass
    return H


class WebhookSafetyTests(SimpleTestCase):
    @override_settings(MODE="shared", SERVER_NETWORKS="")
    def test_a_shared_installation_refuses_private_and_local_addresses_and_plain_http(self):
        for url in ("https://127.0.0.1/hook", "https://10.0.0.5/hook", "https://192.168.1.2/x", "https://169.254.169.254/latest/meta-data",
                    "https://[::1]/hook", "http://93.184.216.34/hook", "ftp://93.184.216.34/x", "https://user:pw@93.184.216.34/hook", "93.184.216.34/hook", ""):
            with self.subTest(url=url), self.assertRaises(recipients.WebhookRefused):
                recipients.check_webhook(url)

    @override_settings(MODE="shared", SERVER_NETWORKS="")
    def test_a_name_that_resolves_inside_is_refused_and_a_public_one_is_pinned(self):
        with mock.patch("watch.recipients.netpolicy.resolve", return_value=[__import__("ipaddress").ip_address("10.1.2.3")]):
            with self.assertRaisesMessage(recipients.WebhookRefused, "private or local"):
                recipients.check_webhook("https://hooks.example.test/x")
        with mock.patch("watch.recipients.netpolicy.resolve", return_value=[__import__("ipaddress").ip_address("93.184.216.34")]):
            self.assertEqual(recipients.check_webhook("https://hooks.example.test/x"), ("https://hooks.example.test/x", "93.184.216.34"))
        with mock.patch("watch.recipients.netpolicy.resolve", return_value=[]):
            with self.assertRaisesMessage(recipients.WebhookRefused, "does not resolve"):
                recipients.check_webhook("https://nowhere.example.test/x")

    @override_settings(MODE="shared", SERVER_NETWORKS="")
    def test_the_connection_goes_to_the_checked_address_not_to_a_new_dns_answer(self):
        with mock.patch("watch.recipients.netpolicy.resolve", return_value=[__import__("ipaddress").ip_address("93.184.216.34")]), \
                mock.patch("watch.recipients.socket.create_connection", side_effect=OSError("stop here")) as connect:
            with self.assertRaises(OSError):
                recipients.post_json("https://hooks.example.test/x", {"text": "t"})
        self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 443))

    @override_settings(MODE="dedicated", SERVER_NETWORKS="any")
    def test_a_message_is_posted_as_json_and_a_redirect_is_not_followed(self):
        seen = []
        ok = Receiver(("127.0.0.1", 0), handler(200, seen=seen))
        redirect = Receiver(("127.0.0.1", 0), handler(302, location="http://127.0.0.1:1/inside"))
        for server in (ok, redirect):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.shutdown)
        recipients.post_json(f"http://127.0.0.1:{ok.server_port}/hook", {"text": "hello"})
        self.assertEqual(seen, ['{"text": "hello"}'])
        with self.assertRaisesMessage(OSError, "redirect"):
            recipients.post_json(f"http://127.0.0.1:{redirect.server_port}/hook", {"text": "hello"})

    def test_only_the_host_of_a_webhook_is_ever_shown(self):
        self.assertEqual(recipients.webhook_host("https://hooks.slack.com/services/T000/B000/SECRET"), "hooks.slack.com")
        self.assertEqual(recipients.webhook_host(""), "")


@override_settings(MODE="shared", **MAIL)
class SettingsPageTests(Fixture):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.admin)

    def post(self, **data):
        return self.client.post(reverse("alert_settings"), data, follow=True)

    def test_the_page_says_where_alerts_go_and_lists_only_my_recipients(self):
        self.confirmed(self.org, "mine@alpha.test")
        self.confirmed(self.other, "theirs@beta.test")
        page = self.client.get(reverse("alert_settings"))
        self.assertContains(page, "mine@alpha.test")
        self.assertNotContains(page, "theirs@beta.test")
        self.assertContains(page, "Alert notifications")

    def test_with_nothing_set_a_shared_installation_says_alerts_stay_in_pgdas(self):
        self.assertContains(self.client.get(reverse("alert_settings")), "no fallback")

    def test_adding_removing_and_resending(self):
        self.assertContains(self.post(action="add", email="ops@example.test"), "confirmation link was sent")
        recipient = AlertRecipient.objects.get(email="ops@example.test")
        cache.clear()
        self.assertContains(self.post(action="resend", recipient=recipient.pk), "sent again")
        self.assertContains(self.post(action="add", email="bad"), "not an email address")
        self.assertContains(self.post(action="remove", recipient=recipient.pk), "Recipient removed")
        self.assertFalse(AlertRecipient.objects.filter(pk=recipient.pk).exists())

    def test_someone_elses_recipient_cannot_be_removed(self):
        theirs = self.confirmed(self.other, "theirs@beta.test")
        self.post(action="remove", recipient=theirs.pk)
        self.assertTrue(AlertRecipient.objects.filter(pk=theirs.pk).exists())

    def test_a_webhook_is_checked_stored_and_shown_by_host_only(self):
        with mock.patch("watch.recipients.netpolicy.resolve", return_value=[__import__("ipaddress").ip_address("93.184.216.34")]):
            page = self.post(action="webhook", webhook="https://hooks.example.test/services/SECRET123")
        self.assertContains(page, "hooks.example.test")
        self.assertNotContains(page, "SECRET123")
        self.org.refresh_from_db()
        self.assertTrue(self.org.alert_webhook_url.endswith("SECRET123"))
        self.assertTrue(AuditEntry.objects.filter(action="alert.webhook").exists())
        self.assertNotIn("SECRET123", AuditEntry.objects.get(action="alert.webhook").detail)
        self.post(action="webhook_remove")
        self.org.refresh_from_db()
        self.assertEqual(self.org.alert_webhook_url, "")

    def test_a_private_webhook_is_refused_with_the_reason(self):
        page = self.post(action="webhook", webhook="https://10.0.0.5/hook")
        self.assertContains(page, "private or local network")
        self.org.refresh_from_db()
        self.assertEqual(self.org.alert_webhook_url, "")

    def test_the_test_button_uses_my_organizations_channels_and_explains_when_there_are_none(self):
        refused = self.client.post(reverse("watch_test_notify"), HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("Alert notifications", refused.json()["error"])
        self.confirmed(self.org, "ops@alpha.test")
        sent = self.client.post(reverse("watch_test_notify"), HTTP_X_REQUESTED_WITH="fetch")
        self.assertTrue(sent.json()["ok"], sent.content)
        self.assertEqual([m.to for m in mail.outbox], [["ops@alpha.test"]])

    def test_only_super_users_can_open_it(self):
        person = get_user_model().objects.create_user("p", email="p@alpha.test", password="pw-12345-abc")
        Membership.objects.create(user=person, organization=self.org, role="editor")
        other = Client()
        other.force_login(person)
        self.assertEqual(other.get(reverse("alert_settings")).status_code, 403)
        self.assertEqual(other.post(reverse("alert_settings"), {"action": "add", "email": "x@example.test"}).status_code, 403)

    def test_the_watch_page_data_names_where_this_servers_alerts_go(self):
        self.confirmed(self.org, "ops@alpha.test")
        payload = self.client.get(reverse("watch_data"), {"server": self.server.pk}).json()
        self.assertEqual((payload["channels"]["source"], payload["channels"]["email"]), ("organization", ["ops@alpha.test"]))


@override_settings(MODE="shared", **MAIL)
class SenderAndFailureTests(Fixture):
    def test_a_sender_with_a_dot_in_its_name_is_written_properly(self):
        import importlib, os
        from unittest import mock as m
        import pgdas.settings as configured
        with m.patch.dict(os.environ, {"PGDAS_ALERT_EMAIL_FROM": "pgdas.org <no-reply@pgdas.org>"}):
            fixed = configured._sender(os.environ["PGDAS_ALERT_EMAIL_FROM"])
        self.assertEqual(fixed, '"pgdas.org" <no-reply@pgdas.org>')
        self.assertEqual(configured._sender("no-reply@pgdas.org"), "no-reply@pgdas.org")
        self.assertEqual(configured._sender("PGDAS <no-reply@pgdas.org>"), "PGDAS <no-reply@pgdas.org>")
        from django.core.mail import EmailMessage
        EmailMessage("s", "b", fixed, ["x@example.test"]).send()                           # the mail library accepts it
        self.assertEqual(len(mail.outbox), 1)

    def test_a_confirmation_that_cannot_be_sent_is_reported_not_pretended(self):
        with mock.patch("watch.recipients.send_mail", side_effect=OSError("smtp down")), self.assertLogs("pgdas.mail", "ERROR"):
            recipient, outcome = recipients.add_recipient(self.org, "ops@example.test")
        self.assertEqual(outcome, "unsent")
        self.assertTrue(AlertRecipient.objects.filter(pk=recipient.pk, verified=False).exists())
        client = Client()
        client.force_login(self.admin)
        with mock.patch("watch.recipients.send_mail", side_effect=OSError("smtp down")), self.assertLogs("pgdas.mail", "ERROR"):
            page = client.post(reverse("alert_settings"), {"action": "add", "email": "second@example.test"}, follow=True)
        self.assertContains(page, "could not be sent just now")
        self.assertNotContains(page, "A confirmation link was sent to second@example.test")