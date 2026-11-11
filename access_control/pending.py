"""Registrations whose emailed link was never opened. They hold an email address, so they are removed once the link has expired."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from audit.models import record

from .models import Membership, Organization, OrganizationServer


def purge(hours):
    """Delete the unconfirmed organizations (and their people) registered more than `hours` ago. Returns how many were removed."""
    cutoff = timezone.now() - timedelta(hours=hours)
    removed = 0
    for organization in Organization.objects.filter(active=False):
        members = list(Membership.objects.filter(organization=organization).select_related("user"))
        waiting = members and all(not m.user.is_active and m.user.date_joined < cutoff for m in members)
        if not waiting or OrganizationServer.objects.filter(organization=organization).exists():
            continue
        with transaction.atomic():
            user_ids = [m.user_id for m in members]
            Membership.objects.filter(organization=organization).delete()
            get_user_model().objects.filter(pk__in=user_ids).delete()
            record(None, "organization.purge", None, f"Unconfirmed registration of {organization.name!r} removed", payload={"organization": organization.pk})
            organization.delete()
        removed += 1
    return removed