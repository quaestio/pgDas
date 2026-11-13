from django.db.models import Q

from . import mode

# Anything that lets a person change a database: with one of these ticked the editing screens (grid, structure dialogs, review popup) are loaded.
WRITE_FIELDS = ("insert", "update", "delete", "truncate", "create", "alter", "drop", "views", "sequences", "schemas", "functions", "triggers",
                "extensions", "maintenance")


def _any_write():
    combined = Q()
    for field in WRITE_FIELDS:
        combined |= Q(**{field: True})
    return combined


def installation(request):
    """For the templates: whether this is a shared installation, the signed-in person's organization (shown only when shared), and whether
    they may change anything at all (Super Users, or anyone with a write permission on some database)."""
    member = getattr(request, "pgdas_member", None)
    can_edit = False
    if member is not None:
        can_edit = member.role == "super_user" or member.database_grants.filter(read=True).filter(_any_write()).exists()
    return {"pgdas_shared": mode.is_shared(), "pgdas_organization": member.organization.name if member is not None else "", "pgdas_can_edit": can_edit}