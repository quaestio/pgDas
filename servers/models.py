from django.conf import settings
from django.db import models

from . import crypto


DEFAULT_DATABASE = "postgres"


class SSHUnlock(models.Model):
    """The secrets of a server that are not kept with it (the PostgreSQL password with "Save password" off, the SSH key's passphrase), held (encrypted) only until `expires_at`. Shared by every process, so web workers, backups and
    alerts all see the same unlock. Expired rows are deleted when they are looked at."""
    server = models.OneToOneField("ServerConnection", on_delete=models.CASCADE, related_name="ssh_unlock")
    passphrase_encrypted = models.TextField(blank=True)            # the SSH key's passphrase, when the key asks for it
    password_encrypted = models.TextField(blank=True)              # the PostgreSQL password, when "Save password" is off
    unlocked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    expires_at = models.DateTimeField()


class ServerConnection(models.Model):
    SSL_MODES = [(mode, mode) for mode in ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")]

    name = models.CharField(max_length=100, unique=True)
    host = models.CharField(max_length=255)
    port = models.PositiveIntegerField(default=5432)
    # Not on the Add/Edit form. PostgreSQL needs some database to log into, so PGDAS uses this one first (older entries
    # may have one saved) and otherwise falls back to postgres and friends; see explorer.pg._connect_first_usable.
    database = models.CharField(max_length=100, blank=True, default=DEFAULT_DATABASE)
    username = models.CharField(max_length=100)
    password_encrypted = models.TextField(blank=True)
    # "Save password" off: the password is not kept; a Super User unlocks the server for a while (see unlock.py), like an SSH key passphrase.
    save_password = models.BooleanField(default=True)
    # Kerberos (GSSAPI) instead of a password: PostgreSQL authenticates the PGDAS process's own Kerberos identity. Needs PGDAS_KERBEROS=1.
    kerberos = models.BooleanField(default=False)
    krbsrvname = models.CharField(max_length=50, blank=True)          # the PostgreSQL service name, when it is not "postgres"
    comments = models.TextField(blank=True)
    sslmode = models.CharField(max_length=20, choices=SSL_MODES, default="prefer")
    # Reaching the server through an SSH server (a "bastion") instead of directly. The host/port above are then as the SSH server sees them
    # (often a private address). The SSH password or private key and its passphrase are stored encrypted, like the PostgreSQL password.
    ssh_enabled = models.BooleanField(default=False)
    ssh_host = models.CharField(max_length=255, blank=True)
    ssh_port = models.PositiveIntegerField(default=22)
    ssh_username = models.CharField(max_length=100, blank=True)
    ssh_auth = models.CharField(max_length=10, choices=[("password", "Password"), ("key", "Private key")], default="password")
    ssh_secret_encrypted = models.TextField(blank=True)             # the SSH password, or the private key text
    ssh_passphrase_encrypted = models.TextField(blank=True)
    # "Ask for the passphrase": it is not kept. A Super User unlocks the server for a while (see unlock.py) and it locks again.
    ssh_prompt_passphrase = models.BooleanField(default=False)
    ssh_host_key = models.TextField(blank=True)                      # "type base64": the SSH server's key, trusted when first connected to
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def sid(self):
        """This server's number as it appears in links and requests: an encrypted token (see servers/ids.py), never the plain number."""
        from . import ids
        return ids.encode(self.pk)

    @property
    def database_name(self):
        """The database PGDAS logs into: the saved one, or postgres when none was given."""
        return (self.database or "").strip() or DEFAULT_DATABASE

    def set_password(self, raw):
        self.password_encrypted = crypto.encrypt(raw)

    def get_password(self):
        typed = getattr(self, "_typed_password", None)                # just typed in the Add server popup, for the test
        if typed is not None:
            return typed
        if self.kerberos:
            return ""                                                  # no password: Kerberos
        if not self.save_password:
            from . import unlock
            value = unlock.current_password(self)
            if value is None:
                raise unlock.SSHLocked(unlock.PASSWORD_LOCKED_MESSAGE)
            return value
        return crypto.decrypt(self.password_encrypted) if self.password_encrypted else ""

    @property
    def asks_for_secret(self):
        """True when something is not kept with the server (the password, or the SSH key passphrase) and a Super User must unlock it."""
        return bool((not self.save_password and not self.kerberos) or (self.ssh_enabled and self.ssh_prompt_passphrase))

    @property
    def unlocked_until(self):
        if not self.asks_for_secret or not self.pk:
            return None
        from . import unlock
        return unlock.until(self)

    @property
    def needs_password_unlock(self):
        return not self.save_password and not self.kerberos

    @property
    def krb_service(self):
        return (self.krbsrvname or "").strip() or "postgres"

    def set_ssh_secret(self, raw):
        self.ssh_secret_encrypted = crypto.encrypt(raw) if raw else ""

    def get_ssh_secret(self):
        return crypto.decrypt(self.ssh_secret_encrypted) if self.ssh_secret_encrypted else ""

    def set_ssh_passphrase(self, raw):
        self.ssh_passphrase_encrypted = crypto.encrypt(raw) if raw else ""

    def get_ssh_passphrase(self):
        typed = getattr(self, "_typed_ssh_passphrase", None)          # just typed in the Add server popup, for the test
        if typed is not None:
            return typed
        if self.ssh_prompt_passphrase:
            from . import unlock
            value = unlock.current(self)
            if value is None:
                raise unlock.SSHLocked(unlock.LOCKED_MESSAGE)
            return value
        return crypto.decrypt(self.ssh_passphrase_encrypted) if self.ssh_passphrase_encrypted else ""

    @property
    def ssh_unlocked_until(self):
        """When the SSH key is unlocked until, for a server that asks for its passphrase; None when it is locked (or does not ask)."""
        if not self.ssh_prompt_passphrase or not self.pk:
            return None
        from . import unlock
        return unlock.until(self)
