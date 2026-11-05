from django.core.checks import Warning, register
from django.db import DatabaseError


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