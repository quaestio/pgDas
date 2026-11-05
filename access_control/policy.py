"""PGDAS authorization only. PostgreSQL must independently authorize execution.

No SQL is executed here, and returning True never proves PostgreSQL privileges.
Read membership and grants afresh per check: cached Django user flags must not
keep a deactivated employee or a revoked assignment authorized.
"""
from django.core.exceptions import PermissionDenied

from .models import DatabaseGrant, Membership, OrganizationServer

DATABASE_ACTIONS = frozenset({"read", "insert", "update", "delete", "truncate", "create", "alter", "drop",
                              "export", "backup_create", "backup_download", "restore"})
ADMIN_ACTIONS = frozenset({"employees_manage", "permissions_manage", "servers_manage", "databases_manage",
                         "postgres_roles_manage", "postgres_grants_manage", "server_settings",
                         "monitor_server", "sessions_manage", "audit_all"})
VIEWER_ACTIONS = frozenset({"read", "export"})
GRANT_ACTIONS = DATABASE_ACTIONS - {"restore"}


def membership_for(user):
    if not getattr(user, "is_authenticated", False) or not user.pk:
        return None
    return Membership.objects.filter(user_id=user.pk, user__is_active=True, organization__active=True).first()


def permits(user, action, *, server=None, database=None, organization=None):
    if action not in DATABASE_ACTIONS | ADMIN_ACTIONS:
        return False
    member = membership_for(user)
    if member is None or member.role not in Membership.Role.values:
        return False
    if organization is not None and member.organization_id != getattr(organization, "pk", organization):
        return False
    if server is not None:
        if not OrganizationServer.objects.filter(server_id=server.pk, organization_id=member.organization_id).exists():
            return False
    elif action in DATABASE_ACTIONS or (action in ADMIN_ACTIONS and organization is None):
        return False
    if action in DATABASE_ACTIONS and (not isinstance(database, str) or not database or "\x00" in database):
        return False
    if member.role == Membership.Role.SUPER_USER:
        return True
    if action in ADMIN_ACTIONS or action == "restore":
        return False
    if member.role == Membership.Role.VIEWER and action not in VIEWER_ACTIONS:
        return False
    grant = DatabaseGrant.objects.filter(membership=member, server=server, database=database, read=True).first()
    return bool(grant and getattr(grant, action, False))


def require(user, action, **scope):
    if not permits(user, action, **scope):
        raise PermissionDenied("You are not permitted to perform this action in the selected scope.")


def require_all(user, actions, **scope):
    """For already classified operations; not a SQL parser. Empty/unknown actions fail closed."""
    actions = tuple(actions)
    if not actions:
        raise PermissionDenied("No supported action was identified.")
    for action in actions:
        require(user, action, **scope)


def default_grants(role):
    """Defaults are copied when assigning a database; never implicit global grants."""
    result = {action: False for action in GRANT_ACTIONS}
    result["read"] = True
    if role == Membership.Role.EDITOR:
        result.update(insert=True, update=True)
    return result


def visible_servers(user):
    from servers.models import ServerConnection
    member = membership_for(user)
    if member is None:
        return ServerConnection.objects.none()
    servers = ServerConnection.objects.filter(organization_scope__organization_id=member.organization_id)
    if member.role == Membership.Role.SUPER_USER:
        return servers
    if member.role not in (Membership.Role.EDITOR, Membership.Role.VIEWER):
        return servers.none()
    return servers.filter(databasegrant__membership=member, databasegrant__read=True).distinct()


def filter_databases(user, server, databases):
    """Intersect names already returned by PostgreSQL with PGDAS read permissions."""
    return [database for database in databases if permits(user, "read", server=server, database=database["name"])]
