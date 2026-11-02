"""Sending alerts: announced once when they start and once when they are over, webhook and email, failures retried."""
import json
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEntry
from explorer.tests import TARGET
from servers.models import ServerConnection

from . import notify
from .models import AlertState


class Receiver(HTTPServer):
    """A tiny web server that remembers what is POSTed to it."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), self.Handler)
        self.received, self.status = [], 200
        threading.Thread(target=self.serve_forever, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.server.received.append(json.loads(body))
            self.send_response(self.server.status)
            self.end_headers()

        def log_message(self, *args):
            pass

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}/services/T000/SECRET-TOKEN"


def alert(key="blocked", title="2 sessions are blocked by locks", level="critical", detail="The longest has waited 3 min."):
    return {"level": level, "title": title, "detail": detail, "key": key}


class NotifyTestCase(TestCase):
    def setUp(self):
        self.hook = Receiver()
        self.addCleanup(self.hook.server_close)
        self.addCleanup(self.hook.shutdown)
        self.server = ServerConnection(name="prod", host=TARGET["HOST"], port=int(TARGET["PORT"]), database="postgres",
                                       username=TARGET["USER"], sslmode="prefer")
        self.server.set_password(TARGET["PASSWORD"])
        self.server.save()
        override = override_settings(ALERT_WEBHOOK_URL=self.hook.url, ALERT_REPEAT_HOURS=12)
        override.enable()
        self.addCleanup(override.disable)
        self.now = timezone.now()

    def check(self, current, hours=0):
        return notify.check_server(self.server, self.now + timedelta(hours=hours), current)

    def texts(self):
        return [m["text"] for m in self.hook.received]


class AnnouncingTests(NotifyTestCase):
    def test_a_new_alert_is_sent_once_and_not_again_while_it_lasts(self):
        summary = self.check([alert()])
        self.assertEqual((summary["new"], summary["sent"]), (1, ["webhook"]))
        self.assertEqual(len(self.hook.received), 1)
        self.assertIn("[CRITICAL] 2 sessions are blocked by locks", self.texts()[0])
        self.assertIn("The longest has waited 3 min.", self.texts()[0])
        self.assertIn("prod", self.texts()[0])
        self.assertEqual(self.hook.received[0]["text"], self.hook.received[0]["content"])        # Slack and Discord both understand it
        self.check([alert()], hours=1)
        self.check([alert()], hours=2)
        self.assertEqual(len(self.hook.received), 1)

    def test_the_same_problem_with_other_numbers_is_not_a_new_alert(self):
        self.check([alert(title="2 sessions are blocked by locks")])
        self.check([alert(title="5 sessions are blocked by locks", detail="The longest has waited 9 min.")], hours=1)
        self.assertEqual(len(self.hook.received), 1)
        self.assertEqual(AlertState.objects.get().title, "5 sessions are blocked by locks")       # the page-side state follows the numbers

    def test_when_it_is_over_a_resolved_message_is_sent_and_a_return_is_new_again(self):
        self.check([alert()])
        self.check([], hours=1)
        self.assertEqual(len(self.hook.received), 2)
        self.assertIn("Resolved", self.texts()[1])
        self.assertIn("[OK] 2 sessions are blocked by locks", self.texts()[1])
        self.assertIsNotNone(AlertState.objects.get().resolved_at)
        self.check([], hours=2)
        self.assertEqual(len(self.hook.received), 2)                                              # nothing more to say
        self.check([alert()], hours=3)
        self.assertEqual(len(self.hook.received), 3)
        self.assertIn("New", self.texts()[2])

    def test_an_alert_that_was_never_announced_is_not_announced_as_resolved(self):
        with override_settings(ALERT_WEBHOOK_URL=""):
            AlertState.objects.create(server=self.server, key="blocked", level="critical", title="t", first_seen=self.now, last_seen=self.now)
        self.check([])
        self.assertEqual(self.hook.received, [])

    def test_critical_alerts_are_repeated_after_the_set_hours_warnings_are_not(self):
        crit, warn = alert(), alert("idle_tx", "1 session has been idle in a transaction too long", "warning", "")
        self.check([crit, warn])
        self.check([crit, warn], hours=11)
        self.assertEqual(len(self.hook.received), 1)
        summary = self.check([crit, warn], hours=13)
        self.assertEqual((summary["repeated"], len(self.hook.received)), (1, 2))
        self.assertIn("Still open", self.texts()[1])
        self.assertIn("blocked by locks", self.texts()[1])
        self.assertNotIn("idle in a transaction", self.texts()[1])
        with override_settings(ALERT_REPEAT_HOURS=0):
            self.check([crit, warn], hours=100)                                                   # 0 = never remind
        self.assertEqual(len(self.hook.received), 2)

    def test_several_new_alerts_make_one_message(self):
        self.check([alert(), alert("slow_query", "1 query has run for over 5 min", "warning", "The longest for 7 min.")])
        self.assertEqual(len(self.hook.received), 1)
        self.assertIn("2 new alerts on prod", self.texts()[0])


class FailureTests(NotifyTestCase):
    def test_a_failed_send_is_recorded_and_tried_again_next_time(self):
        self.hook.status = 500
        summary = self.check([alert()])
        self.assertEqual((summary["sent"], summary["failed"][0][0]), ([], "webhook"))
        self.assertIsNone(AlertState.objects.get().notified_at)
        entry = AuditEntry.objects.get(action="alert.notify")
        self.assertFalse(entry.ok)
        self.assertIn("failed on webhook", entry.detail)
        self.hook.status = 200
        self.check([alert()], hours=1)
        self.assertIsNotNone(AlertState.objects.get().notified_at)
        self.assertEqual(len(self.hook.received), 2)                                              # the 500 one and the retry
        self.assertTrue(AuditEntry.objects.filter(action="alert.notify", ok=True).exists())

    def test_an_unreachable_webhook_does_not_stop_the_other_servers(self):
        with override_settings(ALERT_WEBHOOK_URL="http://127.0.0.1:1/hook"):
            summary = self.check([alert()])
        self.assertTrue(summary["failed"])
        self.assertIsNone(AlertState.objects.get().notified_at)


class ChannelTests(NotifyTestCase):
    def test_email_is_sent_when_a_host_and_recipients_are_set(self):
        with override_settings(ALERT_WEBHOOK_URL="", EMAIL_HOST="smtp.example.test", ALERT_EMAIL_TO=["ops@example.test", "dba@example.test"],
                               ALERT_EMAIL_FROM="PGDAS <pgdas@example.test>"):
            summary = self.check([alert()])
        self.assertEqual(summary["sent"], ["email"])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ops@example.test", "dba@example.test"])
        self.assertEqual(mail.outbox[0].from_email, "PGDAS <pgdas@example.test>")
        self.assertIn("1 new alert on prod", mail.outbox[0].subject)
        self.assertIn("blocked by locks", mail.outbox[0].body)

    def test_recipients_without_a_mail_host_are_ignored(self):
        with override_settings(ALERT_WEBHOOK_URL="", EMAIL_HOST="", ALERT_EMAIL_TO=["ops@example.test"]):
            self.assertEqual(notify.channels(), {"webhook": "", "email": []})
            self.assertIsNone(notify.run())

    def test_the_webhook_address_is_never_shown_only_its_host(self):
        shown = notify.channels()
        self.assertEqual(shown["webhook"], f"127.0.0.1:{self.hook.server_address[1]}")
        self.assertNotIn("SECRET-TOKEN", json.dumps(shown))
        self.check([alert()])
        self.assertNotIn("SECRET-TOKEN", " ".join(AuditEntry.objects.values_list("detail", flat=True)))
        with override_settings(ALERT_WEBHOOK_URL="ftp://example.test/x"):
            self.assertEqual(notify.webhook_host(), "")                                           # only http and https

    def test_nothing_is_remembered_or_sent_while_no_channel_is_set(self):
        with override_settings(ALERT_WEBHOOK_URL=""):
            self.assertIsNone(notify.run())
        self.assertFalse(AlertState.objects.exists())
        self.assertEqual(self.hook.received, [])

    def test_run_watch_sends_for_an_unreachable_server_and_no_notify_stays_quiet(self):
        bad = ServerConnection(name="down", host=TARGET["HOST"], port=1, database="postgres", username="x", sslmode="prefer")
        bad.set_password("x")
        bad.save()
        self.server.delete()
        out = StringIO()
        call_command("run_watch", "--no-notify", stdout=out)
        self.assertEqual(self.hook.received, [])
        call_command("run_watch", stdout=out)
        self.assertEqual(len(self.hook.received), 1)
        self.assertIn("[CRITICAL] The server cannot be reached", self.texts()[0])
        self.assertIn("down: 1 new", out.getvalue())
        call_command("run_watch", stdout=out)
        self.assertEqual(len(self.hook.received), 1)                                              # still down: not announced again


class TestButtonTests(NotifyTestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(get_user_model().objects.create_user("zed", password="pw-12345-abc", is_staff=True))

    def test_the_test_message_goes_to_the_channels_and_is_audited(self):
        response = self.client.post(reverse("watch_test_notify"))
        self.assertEqual(response.json(), {"ok": True, "message": "Test sent on webhook."})
        self.assertIn("test notification", self.texts()[0])
        self.assertIn("zed", self.texts()[0])
        self.assertTrue(AuditEntry.objects.filter(action="alert.test", ok=True).exists())

    def test_a_failing_channel_is_reported_with_the_reason(self):
        self.hook.status = 500
        response = self.client.post(reverse("watch_test_notify"))
        self.assertEqual(response.status_code, 502)
        self.assertIn("webhook", response.json()["error"])
        self.assertTrue(AuditEntry.objects.filter(action="alert.test", ok=False).exists())

    def test_without_a_channel_it_says_what_to_set(self):
        with override_settings(ALERT_WEBHOOK_URL=""):
            response = self.client.post(reverse("watch_test_notify"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("PGDAS_ALERT_WEBHOOK_URL", response.json()["error"])

    def test_only_staff_may_send_it_and_the_page_lists_the_channels(self):
        plain = Client()
        plain.force_login(get_user_model().objects.create_user("plain", password="pw-12345-abc"))
        self.assertEqual(plain.post(reverse("watch_test_notify")).status_code, 302)
        self.assertEqual(self.hook.received, [])
        data = self.client.get(reverse("watch_data"), {"server": self.server.pk}).json()
        self.assertEqual(data["channels"]["webhook"], f"127.0.0.1:{self.hook.server_address[1]}")
