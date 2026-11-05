"""Request-scoped identity used by connection and visibility helpers."""
from contextvars import ContextVar
from copy import copy
from django.core.exceptions import PermissionDenied
from .models import DatabaseGrant
from .policy import membership_for, require, visible_servers

request_context = ContextVar("pgdas_request", default=None)


def user():
    request = request_context.get()
    return request.user if request else None


def is_admin(actor=None):
    member = membership_for(actor or user())
    return bool(member and member.role == "super_user")


def servers():
    from servers.models import ServerConnection
    return visible_servers(user()) if user() else ServerConnection.objects.all()


def default_database(server):
    if user() and not is_admin():
        member = membership_for(user())
        grant = DatabaseGrant.objects.filter(membership=member, server=server, read=True).order_by("database").first()
        if not grant:
            raise PermissionDenied("No database is assigned on this server.")
        return grant.database
    return None


def execution_server(server, database):
    if not user():
        return server
    require(user(), "read", server=server, database=database)
    if is_admin():
        return server
    grant = DatabaseGrant.objects.get(membership=membership_for(user()), server=server, database=database)
    if not grant.execution_username:
        from explorer.pg import PgError
        raise PgError("A Super User must configure a restricted PostgreSQL execution login for this database assignment.")
    result = copy(server)
    result.username = grant.execution_username
    result.password_encrypted = grant.execution_password
    result.database = database
    return result


def audits():
    from django.db.models import Q
    from audit.models import AuditEntry
    actor = user()
    if not actor:
        return AuditEntry.objects.all()
    member = membership_for(actor)
    if not member:
        return AuditEntry.objects.none()
    if is_admin():
        return AuditEntry.objects.filter(Q(server__organization_scope__organization_id=member.organization_id) |
                                        Q(server__isnull=True, user__pgdas_membership__organization_id=member.organization_id))
    allowed = Q(pk__in=[])
    for grant in DatabaseGrant.objects.filter(membership=member, read=True, server__organization_scope__organization_id=member.organization_id):
        allowed |= Q(server_id=grant.server_id, payload__database=grant.database)
    return AuditEntry.objects.filter(allowed, user=actor)


def backups():
    from django.db.models import Q
    from backups.models import Backup
    if not user():
        return Backup.objects.all()
    if is_admin():
        return Backup.objects.filter(server__in=servers())
    allowed = Q(pk__in=[])
    for grant in DatabaseGrant.objects.filter(membership=membership_for(user()), read=True):
        if grant.backup_create or grant.backup_download:
            allowed |= Q(server_id=grant.server_id, database=grant.database)
    return Backup.objects.filter(allowed, server__in=servers())
