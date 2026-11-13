"""Server-wide access: "every database on this server at this level", kept in step with the databases that exist.

A Super User can give a person one level on every database of a server. The grants are ordinary per-database grants, created for each
database that exists; this module creates the missing ones, so databases created later are picked up. It runs when that person signs in,
from the Access page, and from `manage.py sync_access` (put it in a schedule to follow new databases within minutes). A database the Super
User set apart (given None) is left out and stays out.
"""
import logging

from django.utils import timezone

from audit.models import record

from .models import DatabaseGrant, ServerWideAccess
from .policy import preset_permissions

log = logging.getLogger("pgdas.serverwide")


def database_names(server):
    from explorer import pg
    with pg.connect(server) as conn:
        return [row["name"] for row in pg.list_databases(conn)]


def sync_entry(entry, names=None):
    """Create the missing grants for one entry. Returns how many were added."""
    names = database_names(entry.server) if names is None else names
    values = preset_permissions(entry.level)
    values["run_sql"] = entry.run_sql
    added = 0
    for name in names:
        if name in (entry.excluded or []):
            continue
        grant, created = DatabaseGrant.objects.get_or_create(membership=entry.membership, server=entry.server, database=name,
                                                              defaults={"read": True, **values})
        added += created
    entry.synced_at = timezone.now()
    entry.save(update_fields=["synced_at"])
    if added:
        record(None, "permission.sync", entry.server, f"{added} database(s) added to the server-wide access of employee #{entry.membership.user_id}",
               payload={"employee_id": entry.membership.user_id, "added": added})
    return added


def sync_membership(membership):
    """Every server-wide entry of one person. A server that cannot be reached is skipped (and logged), never an error for the caller."""
    total = 0
    for entry in membership.server_wide.select_related("server", "membership"):
        try:
            total += sync_entry(entry)
        except Exception as error:
            log.warning("server-wide access sync for employee #%s on %s failed: %s", membership.user_id, entry.server.name, error)
    return total


def sync_all():
    total = 0
    for entry in ServerWideAccess.objects.select_related("server", "membership", "membership__user").filter(membership__user__is_active=True):
        try:
            total += sync_entry(entry)
        except Exception as error:
            log.warning("server-wide access sync for employee #%s on %s failed: %s", entry.membership.user_id, entry.server.name, error)
    return total