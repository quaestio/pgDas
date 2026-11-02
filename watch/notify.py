"""Sending alerts. `run()` (called by `manage.py run_watch`) compares what is wrong now with what it knew last time and sends:

  * a message when a problem starts (and again every PGDAS_ALERT_REPEAT_HOURS while a critical one lasts),
  * a message when it is over.

Two channels, both set in `.env`: a webhook (Slack, Teams, Discord or anything that takes a JSON POST) and email (SMTP).
Nothing is sent when neither is set. The webhook address is a secret: only its host is ever shown or logged.
"""
import json
import urllib.error
import urllib.request
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.core.mail import EmailMessage
from django.utils import timezone

from audit.models import record
from servers.models import ServerConnection

from . import alerts
from .models import AlertState

LABELS = {"critical": "CRITICAL", "warning": "WARNING"}


def webhook_host():
    url = settings.ALERT_WEBHOOK_URL.strip()
    parts = urlsplit(url)
    return parts.netloc if url and parts.scheme in ("http", "https") and parts.netloc else ""


def email_recipients():
    return [a for a in settings.ALERT_EMAIL_TO if a] if settings.EMAIL_HOST else []


def channels():
    """Which channels are set up, for the page: never the full webhook address."""
    return {"webhook": webhook_host(), "email": email_recipients()}


def _post_webhook(subject, body):
    text = f"{subject}\n{body}"
    request = urllib.request.Request(settings.ALERT_WEBHOOK_URL.strip(), data=json.dumps({"text": text, "content": text}).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "User-Agent": "PGDAS"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:             # noqa: S310 (http/https only, checked by webhook_host)
        if response.status >= 300:
            raise OSError(f"the webhook answered {response.status}")


def send(subject, body):
    """Send one message on every channel that is set up. Returns ([channels that worked], [(channel, reason) that did not])."""
    done, failed = [], []
    if webhook_host():
        try:
            _post_webhook(subject, body)
            done.append("webhook")
        except (OSError, urllib.error.URLError, ValueError) as error:
            failed.append(("webhook", str(error)[:200]))
    recipients = email_recipients()
    if recipients:
        try:
            EmailMessage(subject, body, settings.ALERT_EMAIL_FROM, recipients).send(fail_silently=False)
            done.append("email")
        except Exception as error:                                           # SMTP can fail in many ways; the reason is what matters
            failed.append(("email", str(error)[:200]))
    return done, failed


def _line(alert):
    text = f"[{LABELS.get(alert['level'], alert['level'].upper())}] {alert['title']}"
    return text + (f"\n    {alert['detail']}" if alert.get("detail") else "")


def _compose(server, new, repeated, resolved):
    parts = []
    if new:
        parts.append(f"{len(new)} new alert{'s' if len(new) != 1 else ''} on {server.name}")
    if repeated:
        parts.append(f"{len(repeated)} still open")
    if resolved:
        parts.append(f"{len(resolved)} resolved")
    subject = "PGDAS: " + ", ".join(parts) + (f" ({server.name})" if not new else "")
    lines = []
    for heading, group in (("New", new), ("Still open", repeated)):
        if group:
            lines += [f"{heading}:"] + [_line(a) for a in group] + [""]
    if resolved:
        lines += ["Resolved:"] + [f"[OK] {s.title}" for s in resolved] + [""]
    return subject, "\n".join(lines).strip()


def check_server(server, now=None, current=None):
    """Compare one server's alerts with what was known and send what is due. Returns a short summary dict."""
    now = now or timezone.now()
    current = alerts.evaluate(server) if current is None else current
    states = {s.key: s for s in AlertState.objects.filter(server=server)}
    new, repeated, seen = [], [], set()
    for alert in current:
        key = alert["key"] or alert["title"]
        seen.add(key)
        state = states.get(key)
        if state is None or state.resolved_at is not None:
            state = AlertState.objects.update_or_create(server=server, key=key, defaults={
                "level": alert["level"], "title": alert["title"][:300], "detail": alert["detail"], "first_seen": now, "last_seen": now,
                "notified_at": None, "resolved_at": None})[0]
        else:
            state.level, state.title, state.detail, state.last_seen = alert["level"], alert["title"][:300], alert["detail"], now
            state.save(update_fields=["level", "title", "detail", "last_seen"])
        if state.notified_at is None:
            new.append((alert, state))
        elif (settings.ALERT_REPEAT_HOURS and alert["level"] == "critical"
              and now - state.notified_at >= timedelta(hours=settings.ALERT_REPEAT_HOURS)):
            repeated.append((alert, state))
    resolved = []
    for key, state in states.items():
        if key not in seen and state.resolved_at is None:
            state.resolved_at = now
            state.save(update_fields=["resolved_at"])
            if state.notified_at is not None:                                # only announce the end of something that was announced
                resolved.append(state)
    summary = {"server": server.name, "new": len(new), "repeated": len(repeated), "resolved": len(resolved), "sent": [], "failed": []}
    if not (new or repeated or resolved):
        return summary
    subject, body = _compose(server, [a for a, _ in new], [a for a, _ in repeated], resolved)
    done, failed = send(subject, body)
    summary["sent"], summary["failed"] = done, failed
    if done:
        for _, state in new + repeated:
            state.notified_at = now
            state.save(update_fields=["notified_at"])
    record(None, "alert.notify", server, f"{subject}\nsent: {', '.join(done) or 'nothing'}" +
           "".join(f"\nfailed on {channel}: {reason}" for channel, reason in failed), ok=bool(done) and not failed)
    return summary


def run(now=None):
    """Check every saved server. Does nothing (and remembers nothing) while no channel is set up; once one is, what is wrong at
    that moment is announced once. Returns [summary per server], or None when no channel is set up."""
    if not (webhook_host() or email_recipients()):
        return None
    return [check_server(server, now) for server in ServerConnection.objects.all()]
