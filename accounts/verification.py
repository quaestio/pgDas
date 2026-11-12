"""Verifying the email address of an account that has never proved it (accounts that existed before, or whose address was changed).

When the installation can send email, a correct password is not enough until the address is verified: the sign-in is refused, a link is
emailed (at most one every few minutes) and opening it marks the address verified. Registration and invitation links verify the address
as part of what they do. `manage.py verify_email <address>` marks an address verified by hand (for example when mail is not working).
"""
from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.core.mail import send_mail
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.shortcuts import redirect
from django.urls import reverse

from access_control import mode
from audit.models import record

from .registration import _page

SALT = "pgdas.email-verification"
SESSION_KEY = "pgdas_unverified_user"


def required(user):
    return mode.can_send_email() and not user.email_verified


def send_link(request, user):
    """Email the link, unless one was sent in the last few minutes. Returns True when a message was sent now."""
    if not cache.add(f"pgdas-verify-sent:{user.pk}", 1, settings.EMAIL_VERIFICATION_RESEND_MINUTES * 60):
        return False
    token = signing.dumps({"user": user.pk, "email": user.email}, salt=SALT)
    link = request.build_absolute_uri(reverse("email_verify", args=[token]))
    send_mail("Verify your email address on PGDAS",
              f"Open this link to verify {user.email} and then sign in:\n\n{link}\n\nIt works for {settings.INVITATION_HOURS} hours. "
              "If you did not try to sign in, ignore this message.", settings.ALERT_EMAIL_FROM, [user.email], fail_silently=True)
    return True


def verify(request, token):
    try:
        data = signing.loads(token, salt=SALT, max_age=settings.INVITATION_HOURS * 3600)
    except signing.BadSignature:
        return _page(request, 400, register_active=False, register_blocked="This verification link is not valid or has expired. Sign in again to get a new one.")
    user = get_user_model().objects.filter(pk=data["user"], email=data["email"], deleted_at__isnull=True).first()     # (a changed address voids the link)
    if user is None:
        return _page(request, 400, register_active=False, register_blocked="This verification link is not valid or has expired. Sign in again to get a new one.")
    if user.email_verified:
        messages.info(request, "This address is already verified. Sign in below.")
        return redirect("login")
    if request.method == "POST":
        user.email_verified = True
        user.save(update_fields=["email_verified"])
        record(user, "account.email_verified", None, f"Email address of account #{user.pk} verified")
        messages.success(request, f"{user.email} is verified. Sign in below.")
        return redirect("login")
    return _page(request, register_active=False, verify_email=user.email)

def start(request, user):
    """The password was right but the address is unverified: remember who (in the session) and show the verification page."""
    request.session[SESSION_KEY] = user.pk
    return redirect("email_verify_pending")


def pending(request):
    """The page that says the address must be verified, with a button to send the link again. Only reachable right after a correct password."""
    user = get_user_model().objects.filter(pk=request.session.get(SESSION_KEY), deleted_at__isnull=True).first()
    if user is None or user.email_verified or not mode.can_send_email():
        request.session.pop(SESSION_KEY, None)
        return redirect("login")
    if request.method == "POST":
        if send_link(request, user):
            messages.success(request, f"A new verification link was sent to {user.email}.")
        else:
            messages.info(request, f"A link was sent a moment ago. You can ask for another after {settings.EMAIL_VERIFICATION_RESEND_MINUTES} minutes.")
        return redirect("email_verify_pending")
    return _page(request, register_active=False, verify_pending=user.email)