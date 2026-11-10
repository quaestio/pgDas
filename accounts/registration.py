"""Registering an organization from the public page (shared installations only, and only when PGDAS_ALLOW_ORGANIZATION_REGISTRATION=1).

The visitor becomes the first Super User of the new organization. If the installation can send email (PGDAS_EMAIL_HOST) the
organization and the person are created inactive and a link emailed to the address must be opened to switch them on; without
email they are active at once. Attempts are limited per address, a hidden field catches scripts, and a limit on the number of
organizations (PGDAS_MAX_ORGANIZATIONS) can be set.
"""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache
from django.core.mail import send_mail
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.urls import reverse

from access_control import mode, network
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


def needs_confirmation():
    return bool(settings.EMAIL_HOST)


def register(request):
    if request.user.is_authenticated:
        return redirect("server_list")
    if not mode.registration_open():
        raise Http404                                       # not offered here: dedicated, switched off, or no room left
    form = RegistrationForm(request.POST or None)
    if request.method == "POST":
        if _too_many_attempts(request):
            return _page(request, 429, register_form=form, register_blocked="Too many attempts from this address. Try again later.")
        if form.is_valid():
            with transaction.atomic():
                Installation.objects.select_for_update().get(pk=1)          # one at a time, so the organization limit holds
                if not mode.registration_open():
                    raise Http404
                pending = needs_confirmation()
                organization = Organization.objects.create(name=form.cleaned_data["organization"], active=not pending)
                user = form.save(commit=False)
                user.first_name, user.last_name = form.cleaned_data["name"], ""
                user.is_active = not pending
                user.is_superuser = user.is_staff = True
                user.set_password(form.cleaned_data["password"])
                user.save()
                Membership.objects.create(user=user, organization=organization, role="super_user")
                record(user, "organization.register", None, f"Organization {organization.name!r} registered by {user.email}"
                       + (" (waiting for email confirmation)" if pending else ""),
                       payload={"organization": organization.pk, "pending": pending,
                                "terms": {"version": terms.VERSION, "url": terms.url(), "accepted_at": timezone.now().isoformat(), "address": str(network.client_ip(request))}})
            if pending:
                token = signing.dumps({"organization": organization.pk, "user": user.pk}, salt=SALT)
                link = request.build_absolute_uri(reverse("register_confirm", args=[token]))
                send_mail("Confirm your organization on PGDAS", f"Open this link to confirm {organization.name!r} and sign in:\n\n{link}\n\n"
                          f"It works for {settings.REGISTRATION_CONFIRM_HOURS} hours. If you did not register, ignore this message.",
                          settings.ALERT_EMAIL_FROM, [user.email], fail_silently=True)
                return _page(request, register_sent=user.email)
            messages.success(request, f"Organization {organization.name} was registered. Sign in with {user.email}.")
            return redirect("login")
    return _page(request, register_form=form)


def confirm(request, token):
    try:
        data = signing.loads(token, salt=SALT, max_age=settings.REGISTRATION_CONFIRM_HOURS * 3600)
    except signing.BadSignature:                            # (also an expired link)
        return _page(request, 400, register_active=False, register_form=RegistrationForm(),
                     register_blocked="This confirmation link is not valid or has expired. Register again.")
    with transaction.atomic():
        organization = Organization.objects.select_for_update().filter(pk=data["organization"], active=False).first()
        user = get_user_model().objects.select_for_update().filter(pk=data["user"], is_active=False).first()
        if organization is None or user is None:
            messages.info(request, "This organization is already confirmed (or the registration was removed). Sign in below.")
            return redirect("login")
        organization.active = True
        organization.save(update_fields=["active"])
        user.is_active = True
        user.save(update_fields=["is_active"])
        record(user, "organization.confirm", None, f"Organization {organization.name!r} confirmed by email", payload={"organization": organization.pk})
    messages.success(request, f"Organization {organization.name} is confirmed. Sign in with {user.email}.")
    return redirect("login")