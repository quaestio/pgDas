"""Where an organization's alerts go: its confirmed email recipients and its webhook.

* A recipient is confirmed by its owner (a link is emailed to the address) before it gets anything, so PGDAS cannot be used to send alerts to an
  address that is not the sender's. An address that belongs to a verified member of the organization is confirmed at once. Every alert email ends
  with a link that removes that address.
* A webhook is the organization's own secret address. On a shared installation it must be on the public internet (the same rule as saved servers:
  a customer's webhook must not be a way into the network PGDAS runs in), redirects are not followed, and the connection goes to the address that
  was checked, not to whatever the name resolves to a moment later.
"""
import http.client
import ipaddress
import json
import socket
import ssl
from urllib.parse import urlsplit

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from access_control import mode
from access_control.models import AlertRecipient, Membership
from servers import netpolicy

import logging

log = logging.getLogger("pgdas.mail")

CONFIRM_SALT = "pgdas.alert-recipient-confirm"
STOP_SALT = "pgdas.alert-recipient-stop"
CONFIRM_DAYS = 7


class WebhookRefused(ValueError):
    pass


def base_url():
    """The address people reach PGDAS at, for links in emails that are sent without a web request."""
    if settings.PUBLIC_URL:
        return settings.PUBLIC_URL
    for origin in settings.CSRF_TRUSTED_ORIGINS:
        if origin.startswith("http"):
            return origin.rstrip("/")
    host = next((h for h in settings.ALLOWED_HOSTS if h and not h.startswith(".") and h != "*"), "localhost")
    return ("https://" if settings.BEHIND_HTTPS_PROXY or not settings.DEBUG else "http://") + host


def _link(name, salt, recipient):
    token = signing.dumps({"recipient": recipient.pk, "email": recipient.email}, salt=salt)
    return base_url() + reverse(name, args=[token])


def stop_link(recipient):
    return _link("alert_stop", STOP_SALT, recipient)


# ---- recipients -------------------------------------------------------------------------------------------------------------------------------------

def clean_email(text):
    email = (text or "").strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        raise ValueError("That is not an email address.")
    return email


def add_recipient(organization, text):
    """Add an address. Returns (recipient, "confirmed" | "waiting" | "unsent" | "exists"): "unsent" = saved, but the confirmation email could not be sent now.
    Raises ValueError with a message for the person adding it."""
    email = clean_email(text)
    existing = AlertRecipient.objects.filter(organization=organization, email__iexact=email).first()
    if existing is not None:
        return existing, "exists"
    if AlertRecipient.objects.filter(organization=organization).count() >= settings.ALERT_MAX_RECIPIENTS:
        raise ValueError(f"An organization can have at most {settings.ALERT_MAX_RECIPIENTS} alert recipients.")
    if not cache.add(f"pgdas-alert-add:{organization.pk}", 0, 3600) and cache.incr(f"pgdas-alert-add:{organization.pk}") > 20:
        raise ValueError("Too many addresses were added in the last hour. Try again later.")
    member = Membership.objects.filter(organization=organization, user__email__iexact=email, user__is_active=True, user__email_verified=True).exists()
    recipient = AlertRecipient.objects.create(organization=organization, email=email, verified=member)
    if member:
        return recipient, "confirmed"
    return recipient, ("waiting" if send_confirmation(recipient) else "unsent")


def send_confirmation(recipient):
    """Email the confirmation link (at most one every ten minutes per address). Returns True when a message went out."""
    if not settings.EMAIL_HOST or not cache.add(f"pgdas-alert-confirm:{recipient.pk}", 1, 600):
        return False
    link = _link("alert_confirm", CONFIRM_SALT, recipient)
    try:
        send_mail(f"Confirm alerts from {recipient.organization.name} on PGDAS",
                  f"Somebody at {recipient.organization.name!r} asked PGDAS to send its monitoring alerts to this address.\n\n"
                  f"To receive them, open this link and press Confirm:\n\n{link}\n\n"
                  f"It works for {CONFIRM_DAYS} days. If you do not know about this, ignore this message: nothing is sent to you until you confirm.",
                  settings.ALERT_EMAIL_FROM, [recipient.email], fail_silently=False)
    except Exception as error:                                   # SMTP refused, bad sender, timeout ...: say so instead of pretending
        log.error("alert confirmation email to %s failed: %s: %s", recipient.email, type(error).__name__, error)
        cache.delete(f"pgdas-alert-confirm:{recipient.pk}")
        return False
    recipient.verification_sent_at = timezone.now()
    recipient.save(update_fields=["verification_sent_at"])
    return True


def load(token, salt, max_age=None):
    """The recipient a link is for, or None (forged, expired, or the address was removed or changed since)."""
    try:
        data = signing.loads(token, salt=salt, max_age=max_age)
    except signing.BadSignature:
        return None
    recipient = AlertRecipient.objects.filter(pk=data.get("recipient"), email=data.get("email")).select_related("organization").first()
    return recipient


def confirmed_emails(organization):
    return list(AlertRecipient.objects.filter(organization=organization, verified=True).values_list("email", flat=True)) if settings.EMAIL_HOST else []


# ---- webhook ----------------------------------------------------------------------------------------------------------------------------------------

def check_webhook(url):
    """Returns (url, pinned address or ""). Raises WebhookRefused with a message the person can act on."""
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WebhookRefused("Give the whole address, starting with https://")
    if mode.is_shared() and parts.scheme != "https":
        raise WebhookRefused("On this installation a webhook must use https://")
    if parts.username or parts.password:
        raise WebhookRefused("Put no user name or password in the address; use the secret part of the address the service gave you.")
    host = parts.hostname
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        addresses = netpolicy.resolve(host)
    if not addresses:
        raise WebhookRefused("That host name does not resolve to an address.")
    restricted = mode.is_shared() or netpolicy.policy() == "public"
    if restricted and not all(a.is_global for a in addresses):
        raise WebhookRefused("That address is on a private or local network, which this installation does not allow for webhooks.")
    return url, str(addresses[0])


class _Pinned(http.client.HTTPConnection):
    """Connects to the address that was checked (not to a fresh DNS answer), and for https still verifies the certificate for the real host name."""

    def __init__(self, host, port, pinned, tls, timeout):
        super().__init__(host, port, timeout=timeout)
        self._pinned, self._tls = pinned, tls

    def connect(self):
        sock = socket.create_connection((self._pinned, self.port), self.timeout)
        self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host) if self._tls else sock


def post_json(url, payload, timeout=10):
    """POST a JSON message to a customer's webhook, safely. Raises WebhookRefused or OSError with the reason."""
    url, pinned = check_webhook(url)
    parts = urlsplit(url)
    tls = parts.scheme == "https"
    connection = _Pinned(parts.hostname, parts.port or (443 if tls else 80), pinned, tls, timeout)
    try:
        connection.request("POST", (parts.path or "/") + ("?" + parts.query if parts.query else ""), json.dumps(payload).encode("utf-8"),
                           {"Content-Type": "application/json", "User-Agent": "PGDAS", "Host": parts.netloc})
        status = connection.getresponse().status
    finally:
        connection.close()
    if 300 <= status < 400:
        raise OSError(f"the webhook answered {status} (a redirect: PGDAS does not follow redirects)")
    if status >= 400:
        raise OSError(f"the webhook answered {status}")


def webhook_host(url):
    parts = urlsplit((url or "").strip())
    return parts.netloc.split("@")[-1] if parts.scheme in ("http", "https") and parts.netloc else ""