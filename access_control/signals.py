from . import serverwide
from .policy import membership_for


def follow_new_databases(sender, request, user, **kwargs):
    """At sign-in, pick up databases created since the last time for people with server-wide access (never blocks the sign-in)."""
    try:
        member = membership_for(user)
        if member is not None and member.server_wide.exists():
            serverwide.sync_membership(member)
    except Exception:
        pass