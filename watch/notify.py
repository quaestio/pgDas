"""Sending alerts. `run()` (called by `manage.py run_watch`) compares what is wrong now with what it knew last time and sends:

  * a message when a problem starts (and again every PGDAS_ALERT_REPEAT_HOURS while a critical one lasts),
  * a message when it is over.

WHERE a server's alerts go depends on its organization:
  * An organization sets its own recipients (confirmed by their owners) and its own webhook (Alert notifications page). Its alerts go only there.
  * An organization with nothing set: on a DEDICATED installation the installation's own channels in `.env` are used (a webhook and an email list);
    on a SHARED installation nothing is sent, because the installation's list belongs to the person who runs PGDAS, not to the organization.
Nothing is sent, and nothing remembered, for a server that has no channel. An organization can send at most PGDAS_ALERT_MAX_PER_HOUR messages an hour.
The webhook addresses are secrets: only their host is ever shown or logged.
"""
import json
import urllib.error
import urllib.request
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.core.cache import cache
from django.core.mail import EmailMessage
from django.utils import timezone

from access_control import mode
from access_control.models import OrganizationServer
from audit.models import record
from servers.models import ServerConnection

from . import alerts, recipients
from .models import AlertState

LABELS = {"critical": "CRITICAL", "warning": "WARNING"}


# ---- the installation's own channels (.env) ---------------------------------------------------------------------------------------------------------

def webhook_host():
    url = settings.ALERT_WEBHOOK_URL.strip()
    parts = urlsplit(url)
    return parts.netloc if url and parts.scheme in ("http", "https") and parts.netloc else ""


def email_recipients():
    return [a for a in settings.ALERT_EMAIL_TO if a] if settings.EMAIL_HOST else []


def installation_channels():
    return {"source": "installation", "organization": None, "webhook_url": settings.ALERT_WEBHOOK_URL.strip() if webhook_host() else "", "restricted": False,
            "emails": email_recipients(), "recipients": []}


# ---- an organization's channels ---------------------------------------------------------------------------------------------------------------------

def organization_channels(organization):
    from access_control.models import AlertRecipient
    waiting = AlertRecipient.objects.filter(organization=organization, verified=False).count()
    return {"source": "organization", "organization": organization, "webhook_url": (organization.alert_webhook_url or "").strip(), "restricted": True,
            "emails": recipients.confirmed_emails(organization), "recipients": list(AlertRecipient.objects.filter(organization=organization, verified=True)),
            "waiting": waiting}


def _empty(channels):
    return not (channels["webhook_url"] or channels["emails"])


def channels_for_organization(organization):
    """The channels that carry an organization's alerts (see the module text for the rules). `source` says where they come from: organization, installation or none."""
    if organization is not None:
        own = organization_channels(organization)
        if not _empty(own):
            return own
    if not mode.is_shared():
        fallback = installation_channels()
        if not _empty(fallback):
            return fallback
    return {"source": "none", "organization": organization, "webhook_url": "", "restricted": False, "emails": [], "recipients": []}


def channels_for(server):
    scope = OrganizationServer.objects.filter(server=server).select_related("organization").first()
    return channels_for_organization(scope.organization if scope else None)


def channels(server=None):
    """Which channels are set up, for the page: never the full webhook address. With a server: the ones that carry its alerts."""
    chosen = channels_for(server) if server is not None else installation_channels()
    host = recipients.webhook_host(chosen["webhook_url"]) if chosen["webhook_url"] else ""
    return {"webhook": host, "email": list(chosen["emails"]), "source": chosen["source"]}


# ---- sending ----------------------------------------------------------------------------------------------------------------------------------------

def _post_webhook(subject, body):
    text = f"{subject}\n{body}"
    request = urllib.request.Request(settings.ALERT_WEBHOOK_URL.strip(), data=json.dumps({"text": text, "content": text}).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "User-Agent": "PGDAS"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:             # noqa: S310 (http/https only, checked by webhook_host)
        if response.status >= 300:
            raise OSError(f"the webhook answered {response.status}")


def send(subject, body, chosen=None):
    """Send one message on every channel in `chosen` (default: the installation's own). Returns ([channels that worked], [(channel, reason) that did not])."""
    chosen = chosen or installation_channels()
    done, failed = [], []
    if chosen["webhook_url"]:
        try:
            if chosen["restricted"]:
                text = f"{subject}\n{body}"
                recipients.post_json(chosen["webhook_url"], {"text": text, "content": text})
            else:
                _post_webhook(subject, body)
            done.append("webhook")
        except (OSError, urllib.error.URLError, ValueError) as error:
            failed.append(("webhook", str(error)[:200]))
    if chosen["emails"]:
        try:
            if chosen["recipients"]:
                for recipient in chosen["recipients"]:                         # one message each, so each carries its own link to stop
                    footer = (f"\n\n--\nYou receive this because {recipient.organization.name} sends its PGDAS alerts to this address. "
                              f"To stop: {recipients.stop_link(recipient)}")
                    EmailMessage(subject, body + footer, settings.ALERT_EMAIL_FROM, [recipient.email]).send(fail_silently=False)
            else:
                EmailMessage(subject, body, settings.ALERT_EMAIL_FROM, chosen["emails"]).send(fail_silently=False)
            done.append("email")
        except Exception as error:                                           # SMTP can fail in many ways; the reason is what matters
            failed.append(("email", str(error)[:200]))
    return done, failed


def _within_limit(chosen):
    """An organization may send at most ALERT_MAX_PER_HOUR messages an hour (the installation's own channels are not limited)."""
    organization = chosen.get("organization")
    if organization is None:
        return True
    key = f"pgdas-alert-sent:{organization.pk}:{timezone.now():%Y%m%d%H}"
    cache.add(key, 0, 3700)
    return cache.incr(key) <= settings.ALERT_MAX_PER_HOUR


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


def check_server(server, now=None, current=None, chosen=None):
    """Compare one server's alerts with what was known and send what is due. Returns a short summary dict."""
    now = now or timezone.now()
    chosen = chosen or channels_for(server)
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
    if not _within_limit(chosen):
        summary["failed"] = [("limit", f"{chosen['organization'].name} reached {settings.ALERT_MAX_PER_HOUR} messages this hour; it will be tried again next time")]
        record(None, "alert.notify", server, f"{subject}\nnot sent: the hourly limit of the organization was reached", ok=False)
        return summary
    done, failed = send(subject, body, chosen)
    summary["sent"], summary["failed"] = done, failed
    if done:
        for _, state in new + repeated:
            state.notified_at = now
            state.save(update_fields=["notified_at"])
    record(None, "alert.notify", server, f"{subject}\nsent: {', '.join(done) or 'nothing'}" +
           "".join(f"\nfailed on {channel}: {reason}" for channel, reason in failed), ok=bool(done) and not failed)
    return summary


def run(now=None):
    """Check every saved server that has a channel. Does nothing (and remembers nothing) for a server with none; once one is set up, what is wrong at
    that moment is announced once. Returns [summary per server], or None when no server has a channel."""
    results = []
    for server in ServerConnection.objects.all():
        chosen = channels_for(server)
        if _empty(chosen):
            continue
        results.append(check_server(server, now, chosen=chosen))
    return results or None