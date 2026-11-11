"""Inviting a person to an organization: they get an emailed link, confirm their address there and choose their own password.
Until they do, the person cannot sign in (no password is set and the account is switched off), so nobody can be given an address
that is not theirs and an administrator never knows anyone's password."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import SetPasswordForm
from django.core import signing
from django.core.mail import send_mail
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse

from access_control import mode
from access_control.models import Membership
from audit.models import record

from . import terms
from .forms import EmailAuthenticationForm

SALT = "pgdas.employee-invitation"


def send(request, user, organization):
    token = signing.dumps({"user": user.pk}, salt=SALT)
    link = request.build_absolute_uri(reverse("invitation_accept", args=[token]))
    send_mail(f"You are invited to {organization.name} on PGDAS",
              f"{organization.name!r} invited you to PGDAS. Open this link to confirm your email address and choose your password:\n\n{link}\n\n"
              f"It works for {settings.INVITATION_HOURS} hours. If you were not expecting this, ignore this message and nothing happens.",
              settings.ALERT_EMAIL_FROM, [user.email], fail_silently=True)


def _waiting(user_id):
    user = get_user_model().objects.filter(pk=user_id, is_active=False, deleted_at__isnull=True).first()
    return user if user is not None and not user.has_usable_password() and Membership.objects.filter(user=user).exists() else None


def _page(request, status=200, **extra):
    context = {"form": EmailAuthenticationForm(request), "registration_open": mode.registration_open(), "terms_url": terms.url(), **extra}
    return render(request, "registration/login.html", context, status=status)


def accept(request, token):
    try:
        data = signing.loads(token, salt=SALT, max_age=settings.INVITATION_HOURS * 3600)
    except signing.BadSignature:
        return _page(request, 400, register_blocked="This invitation link is not valid or has expired. Ask your administrator to send it again.")
    user = _waiting(data["user"])
    if user is None:
        messages.info(request, "This invitation was already used (or removed). Sign in below.")
        return redirect("login")
    form = SetPasswordForm(user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = get_user_model().objects.select_for_update().get(pk=user.pk)
            if user.is_active or user.has_usable_password():
                messages.info(request, "This invitation was already used. Sign in below.")
                return redirect("login")
            user.set_password(form.cleaned_data["new_password1"])
            user.is_active = True
            user.email_verified = True
            user.save()
            record(user, "employee.accept", None, f"Employee #{user.pk} confirmed their email and chose a password")
        messages.success(request, f"Your account is ready. Sign in with {user.email}.")
        return redirect("login")
    organization = Membership.objects.select_related("organization").get(user=user).organization
    return _page(request, confirm_form=form, confirm_email=user.email, confirm_organization=organization.name, confirm_button="CONFIRM")