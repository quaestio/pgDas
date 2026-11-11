from django.conf import settings
from django.core.checks import Warning, register
from django.db import DatabaseError


@register()
def sqlite_is_for_trying_it_out(app_configs, **kwargs):
    if getattr(settings, "APP_DB_ENGINE", "") == "sqlite" and not settings.DEBUG:
        return [Warning("PGDAS keeps its own data in a SQLite file.",
                        hint="SQLite allows one writer at a time, so people changing things together can meet 'database is locked'. "
                             "Use PGDAS_APP_DB_ENGINE=postgresql for a shared installation.", id="access_control.W002")]
    return []


@register()
def registration_needs_email(app_configs, **kwargs):
    from . import mode
    if settings.MODE == "shared" and settings.ALLOW_ORGANIZATION_REGISTRATION and not mode.can_send_email():
        return [Warning("Registration is switched on but this installation cannot send email, so /register/ stays closed.",
                        hint="Set PGDAS_EMAIL_HOST (and the other PGDAS_EMAIL_* settings): the person must confirm their address and choose their password from an emailed link.",
                        id="access_control.W004")]
    return []


@register()
def shared_installation_reaches_private_networks(app_configs, **kwargs):
    from servers import netpolicy
    if settings.MODE == "shared" and netpolicy.policy() == "any":
        return [Warning("This shared installation lets saved servers be on private and local networks (PGDAS_SERVER_NETWORKS=any).",
                        hint="Anyone who becomes a Super User of an organization can then make PGDAS connect to your internal network. Use PGDAS_SERVER_NETWORKS=public and list your own servers in PGDAS_ALLOWED_SERVER_HOSTS.",
                        id="access_control.W005")]
    return []


@register()
def dedicated_means_one_organization(app_configs, **kwargs):
    from . import mode
    try:
        crowded = mode.too_many_for_dedicated()
        count = mode.organization_count() if crowded else 0
    except DatabaseError:
        return []
    if not crowded:
        return []
    return [Warning(f"PGDAS_MODE is dedicated but {count} organizations exist.",
                    hint="Creating and registering organizations is switched off. Set PGDAS_MODE=shared in .env if this installation should hold several.",
                    id="access_control.W003")]


@register()
def every_organization_has_a_super_user(app_configs, **kwargs):
    """An organization needs at least one active Super User: nobody else can add people, servers or settings."""
    from .models import Membership, Organization
    try:
        with_one = Membership.objects.filter(role="super_user", user__is_active=True).values("organization_id")
        lonely = list(Organization.objects.filter(active=True).exclude(pk__in=with_one).values_list("name", flat=True))
    except DatabaseError:
        return []                                   # the tables are not there yet (before the first migrate)
    return [Warning(f"The organization {name!r} has no active Super User.", hint=f"Run: python manage.py create_superuser --organization \"{name}\"",
                    id="access_control.W001") for name in lonely]