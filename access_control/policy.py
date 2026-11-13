"""PGDAS authorization only. PostgreSQL must independently authorize execution.

No SQL is executed here, and returning True never proves PostgreSQL privileges.
Read membership and grants afresh per check: cached Django user flags must not
keep a deactivated employee or a revoked assignment authorized.
"""
from django.core.exceptions import PermissionDenied

from .models import DatabaseGrant, Membership, OrganizationServer

DATABASE_ACTIONS = frozenset({"read", "insert", "update", "delete", "truncate", "create", "alter", "drop",
                              "export", "backup_create", "backup_download", "restore",
                              "run_sql", "views", "sequences", "schemas", "functions", "triggers", "extensions", "maintenance"})
ADMIN_ACTIONS = frozenset({"employees_manage", "permissions_manage", "servers_manage", "databases_manage",
                         "postgres_roles_manage", "postgres_grants_manage", "server_settings",
                         "monitor_server", "sessions_manage", "audit_all"})
VIEWER_ACTIONS = frozenset({"read", "export"})
# Function bodies, triggers and extensions run with the connecting login's power and cannot be read by PGDAS: they take effect only on an
# assignment that has its own restricted PostgreSQL login (so PostgreSQL itself limits what they can do).
NEEDS_RESTRICTED_LOGIN = frozenset({"functions", "triggers", "extensions"})
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
    grant = DatabaseGrant.objects.filter(membership=member, server=server, database=database, read=True).first()
    if grant and action in NEEDS_RESTRICTED_LOGIN and not grant.execution_username:
        return False
    return bool(grant and getattr(grant, action, False))


ACTION_WORDS = {"read": "view data", "insert": "add rows", "update": "change rows", "delete": "delete rows", "truncate": "empty tables",
               "create": "create objects", "alter": "alter objects", "drop": "drop objects", "export": "export data",
               "backup_create": "create backups", "backup_download": "download backups", "restore": "restore a backup",
               "run_sql": "run your own SQL", "views": "create or drop views", "sequences": "create or change sequences",
               "schemas": "create or drop schemas", "functions": "create or change functions", "triggers": "create or drop triggers",
               "extensions": "create or drop extensions", "maintenance": "run maintenance (vacuum, analyze, reindex)"}
# What a person can be given on one database. Viewer and Editor are not people: the same employee can be a viewer on one database and an
# editor on another. "custom" is any other combination of the tick boxes.
PRESETS = {
    "viewer": {"read", "export"},
    "editor": {"read", "export", "insert", "update", "delete"},
    "designer": {"read", "export", "insert", "update", "delete", "truncate", "create", "alter", "drop", "views", "sequences", "schemas"},
}
# Ticked next to a level, not part of it: running one's own SQL, and the three that need a restricted login.
EXTRAS = frozenset({"run_sql", "functions", "triggers", "extensions", "maintenance"})
LEVEL_LABELS = {"viewer": "Viewer", "editor": "Editor", "designer": "Designer", "custom": "Custom"}


def preset_permissions(level):
    allowed = PRESETS[level]
    return {action: action in allowed for action in GRANT_ACTIONS if action not in EXTRAS}


def level_of(grant):
    """The preset a grant matches exactly, else "custom"."""
    have = {action for action in GRANT_ACTIONS if action not in EXTRAS and getattr(grant, action, False)}
    for level, allowed in PRESETS.items():
        if have == allowed:
            return level
    return "custom"


def refusal_message(user, action, *, server=None, database=None, organization=None):
    """Why PGDAS (the first gate) refuses: worded so the person knows what to ask for. The second gate is PostgreSQL itself (see explorer.pg)."""
    member = membership_for(user)
    where = f"{database!r} on {server.name!r}" if server is not None and database else "this server" if server is not None else "this organization"
    if member is None:
        return "Your account has no active membership in an organization. Contact your Super User."
    if action in ADMIN_ACTIONS:
        return "PGDAS blocked this: it is reserved for Super Users of your organization."
    if action == "restore":
        return "PGDAS blocked this: restoring a backup is reserved for Super Users."
    grant = DatabaseGrant.objects.filter(membership=member, server=server, database=database).first() if server is not None and database else None
    if grant is None or not grant.read:
        return f"PGDAS blocked this: you have no access to {where}. Ask a Super User to give you access."
    if action in NEEDS_RESTRICTED_LOGIN and getattr(grant, action, False) and not grant.execution_username:
        return (f"PGDAS blocked this: {ACTION_WORDS[action]} needs a restricted PostgreSQL login on {where}, because PGDAS cannot check what a function, "
                "trigger or extension does. Ask a Super User to set a restricted login for your access.")
    level = LEVEL_LABELS[level_of(grant)]
    return (f"PGDAS blocked this: your access to {where} is {level}, which does not allow this ({ACTION_WORDS.get(action, action)}). "
            "Ask a Super User to change your access.")


def require(user, action, **scope):
    if not permits(user, action, **scope):
        raise PermissionDenied(refusal_message(user, action, **scope))


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
