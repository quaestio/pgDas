"""The installation's mode, from .env (PGDAS_MODE): dedicated (one organization) or shared (many). Nothing in the app can change it."""
from django.conf import settings

from .models import Organization


def is_shared():
    return settings.MODE == "shared"


def organization_count():
    return Organization.objects.count()


def room_for_another():
    """True while the installation may hold one more organization (shared mode, below PGDAS_MAX_ORGANIZATIONS if one is set)."""
    if not is_shared():
        return False
    limit = settings.MAX_ORGANIZATIONS
    return not limit or organization_count() < limit


def registration_open():
    """Whether the public page /register/ works: shared mode, switched on in .env, and room for another organization."""
    return is_shared() and settings.ALLOW_ORGANIZATION_REGISTRATION and room_for_another()


def too_many_for_dedicated():
    """A dedicated installation that nevertheless holds several organizations (the mode was changed after they were made)."""
    return not is_shared() and organization_count() > 1