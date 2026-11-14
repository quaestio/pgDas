"""Unlocking a server whose SSH key asks for its passphrase.

The passphrase is not stored with the server. A Super User types it once, which proves it opens the key, and it is kept (encrypted, like every
secret here) for SSH_UNLOCK_HOURS; then it is deleted. While unlocked, everything that connects to that server works (employees, backups, alerts).
While locked, those connections fail with a message saying a Super User must unlock it.
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from . import crypto

LOCKED_MESSAGE = ("The SSH key of this server is locked. A Super User must unlock it (open Servers, press Unlock next to the server and enter the key's "
                  "passphrase). It stays unlocked for a few hours and then locks again.")


class SSHLocked(Exception):
    pass


def _row(server):
    from .models import SSHUnlock
    row = SSHUnlock.objects.filter(server_id=server.pk).first()
    if row is not None and row.expires_at <= timezone.now():
        row.delete()                                         # expired: gone, not just ignored
        return None
    return row


def current(server):
    """The passphrase while unlocked, else None."""
    row = _row(server) if server.pk else None
    return crypto.decrypt(row.passphrase_encrypted) if row is not None else None


def until(server):
    row = _row(server)
    return row.expires_at if row is not None else None


def unlock(server, passphrase, user=None, hours=None):
    from .models import SSHUnlock
    expires = timezone.now() + timedelta(hours=hours or settings.SSH_UNLOCK_HOURS)
    SSHUnlock.objects.update_or_create(server=server, defaults={"passphrase_encrypted": crypto.encrypt(passphrase), "unlocked_by": user, "expires_at": expires})
    return expires


def lock(server):
    from .models import SSHUnlock
    SSHUnlock.objects.filter(server_id=server.pk).delete()


def purge():
    """Delete every expired unlock (run from run_backups, so they do not linger on a quiet installation)."""
    from .models import SSHUnlock
    return SSHUnlock.objects.filter(expires_at__lte=timezone.now()).delete()[0]