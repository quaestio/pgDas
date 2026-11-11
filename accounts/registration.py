"""Registering an organization from the public page (shared installations only, PGDAS_ALLOW_ORGANIZATION_REGISTRATION=1, and only when
the installation can send email).

The visitor enters the organization and their own details, with no password. The organization and the person are created switched off
and a link is emailed to the address. Opening the link shows a form to choose a password; submitting it switches both on. So whoever
registers must own the mailbox: nobody can register, or take over, an address that is not theirs. An unconfirmed registration is
removed once the link has expired, which frees the address. Attempts are limited per address, a hidden field catches scripts, and a
limit on the number of organizations (PGDAS_MAX_ORGANIZATIONS) can be set.
"""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import SetPasswordForm
from django.core import signing
from django.core.cache import cache
from django.core.mail import send_mail
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from access_control import mode, network, pending
from access_control.models import Installation, Membership, Organization
from audit.models import record

from . import terms
from .forms import EmailAuthenticationForm, RegistrationForm

SALT = "pgdas.organization-registration"


def _too_many_attempts(request):
    key = f"pgdas-registration:{network.client_ip(request)}"
    cache.add(key, 0, 3600)
    return cache.incr(key) > settings.REGISTRATIONS_PER_HOUR


def _page(request, status=200, **extra):
    """Registering happens on the sign-in page (its second tab), so every answer is that page."""
    context = {"form": EmailAuthenticationForm(request), "registration_open": mode.registration_open(), "terms_url": terms.url(), "register_active": True, **extra}
    return render(request, "registration/login.html", context, status=status)


def register(request):
    if request.user.is_authenticated:
        return redirect("server_list")
    if not mode.registration_open():
        raise Http404                                       # not offered here: dedicated, switched off, no email, or no room left
    pending.purge(settings.REGISTRATION_CONFIRM_HOURS)      # registrations whose link expired give their address (and their place) back
    form = RegistrationForm(request.POST or None)
    if request.method == "POST":
        if _too_many_attempts(request):
            return _page(request, 429, register_form=form, register_blocked="Too many attempts from this address. Try again later.")
        if form.is_valid():
            with transaction.atomic():
                Installation.objects.select_for_update().get(pk=1)          # one at a time, so the organization limit holds
                if not mode.registration_open():
                    raise Http404
                organization = Organization.objects.create(name=form.cleaned_data["organization"], active=False)
                user = get_user_model()(email=form.cleaned_data["email"], first_name=form.cleaned_data["name"], last_name="",
                                        designation=form.cleaned_data["designation"], is_active=False, is_staff=True, is_superuser=True)
                user.set_unusable_password()                                # the person chooses it from the emailed link
                user.save()
                Membership.objects.create(user=user, organization=organization, role="super_user")
                record(user, "organization.register", None, f"Organization {organization.name!r} registered by {user.email} (waiting for email confirmation)",
                       payload={"organization": organization.pk, "pending": True,
                                "terms": {"version": terms.VERSION, "url": terms.url(), "accepted_at": timezone.now().isoformat(),
                                          "address": str(network.client_ip(request))}})
            token = signing.dumps({"organization": organization.pk, "user": user.pk}, salt=SALT)
            link = request.build_absolute_uri(reverse("register_confirm", args=[token]))
            send_mail("Confirm your organization on PGDAS",
                      f"Open this link to confirm {organization.name!r} and choose your password:\n\n{link}\n\n"
                      f"It works for {settings.REGISTRATION_CONFIRM_HOURS} hours. If you did not register, ignore this message and nothing happens.",
                      settings.ALERT_EMAIL_FROM, [user.email], fail_silently=True)
            return _page(request, register_sent=user.email)
    return _page(request, register_form=form)


def _pending(data):
    organization = Organization.objects.filter(pk=data["organization"], active=False).first()
    user = get_user_model().objects.filter(pk=data["user"], is_active=False).first()
    return (organization, user) if organization is not None and user is not None else (None, None)


def confirm(request, token):
    """The emailed link: shows a form to choose the password; submitting it switches the organization and the person on."""
    try:
        data = signing.loads(token, salt=SALT, max_age=settings.REGISTRATION_CONFIRM_HOURS * 3600)
    except signing.BadSignature:                            # (also an expired link)
        return _page(request, 400, register_active=False, register_form=RegistrationForm() if mode.registration_open() else None,
                     register_blocked="This confirmation link is not valid or has expired. Register again.")
    organization, user = _pending(data)
    if organization is None:
        messages.info(request, "This organization is already confirmed (or the registration was removed). Sign in below.")
        return redirect("login")
    form = SetPasswordForm(user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            organization, user = Organization.objects.select_for_update().filter(pk=organization.pk, active=False).first(), \
                get_user_model().objects.select_for_update().filter(pk=user.pk, is_active=False).first()
            if organization is None or user is None:
                messages.info(request, "This organization is already confirmed. Sign in below.")
                return redirect("login")
            organization.active = True
            organization.save(update_fields=["active"])
            user.set_password(form.cleaned_data["new_password1"])
            user.is_active = True
            user.email_verified = True
            user.save()
            record(user, "organization.confirm", None, f"Organization {organization.name!r} confirmed by email", payload={"organization": organization.pk})
        messages.success(request, f"Organization {organization.name} is confirmed. Sign in with {user.email}.")
        return redirect("login")
    return _page(request, register_active=False, confirm_form=form, confirm_email=user.email, confirm_organization=organization.name)
