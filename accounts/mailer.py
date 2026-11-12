"""Sending the emails PGDAS needs (registration, invitation, verification) without hiding failures: a failure is logged (the service log) and
audited, and the caller is told, so the page can say the email could not be sent instead of pretending."""
import logging

from django.conf import settings
from django.core.mail import send_mail

from audit.models import record

log = logging.getLogger("pgdas.mail")


def send(subject, body, recipient, *, purpose):
    """True when the mail server accepted the message. (Accepted is not the same as delivered: spam folders and SPF/DKIM are outside PGDAS.)"""
    try:
        send_mail(subject, body, settings.ALERT_EMAIL_FROM, [recipient], fail_silently=False)
    except Exception as error:                                   # SMTP refused, timeout, bad login, TLS mismatch, DNS ...
        log.error("email (%s) to %s failed: %s: %s", purpose, recipient, type(error).__name__, error)
        try:
            record(None, "email.failed", None, f"{purpose} email to {recipient} could not be sent: {type(error).__name__}", ok=False)
        except Exception:
            pass
        return False
    return True