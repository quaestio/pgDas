"""Audited grant changes for later employee screens. No automatic PostgreSQL GRANTs."""
from django.core.exceptions import ValidationError
from django.db import transaction

from audit.models import record
from .models import DatabaseGrant, Membership
from .policy import GRANT_ACTIONS, VIEWER_ACTIONS, default_grants, require


@transaction.atomic
def assign_database(actor, employee, server, database, permissions=None):
    employee = Membership.objects.select_for_update().get(pk=employee.pk)
    require(actor, "permissions_manage", organization=employee.organization_id, server=server)
    if employee.role == Membership.Role.SUPER_USER:
        raise ValidationError("Super Users already have organization-wide access.")
    values = default_grants(employee.role) if permissions is None else {action: False for action in GRANT_ACTIONS}
    if permissions is not None:
        if set(permissions) - GRANT_ACTIONS or any(type(value) is not bool for value in permissions.values()):
            raise ValidationError("Use supported actions and boolean permission values.")
        values.update(permissions)
    # Validate names before using them in SQL parameters (PostgreSQL rejects NUL text).
    DatabaseGrant(membership=employee, server=server, database=database).clean()
    grant, _ = DatabaseGrant.objects.get_or_create(membership=employee, server=server, database=database)
    for action, value in values.items():
        setattr(grant, action, value)
    grant.full_clean()
    grant.save()
    record(actor, "permission.assign", server, f"Database permissions updated for employee #{employee.user_id}",
           payload={"database": database, "employee_id": employee.user_id, "permissions": values})
    return grant


@transaction.atomic
def revoke_database(actor, employee, server, database):
    employee = Membership.objects.select_for_update().get(pk=employee.pk)
    require(actor, "permissions_manage", organization=employee.organization_id, server=server)
    DatabaseGrant.objects.filter(membership=employee, server=server, database=database).delete()
    record(actor, "permission.revoke", server, f"Database assignment revoked for employee #{employee.user_id}",
           payload={"database": database, "employee_id": employee.user_id})
