from django.conf import settings
from django.db import models

from . import crypto


DEFAULT_DATABASE = "postgres"


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
    ssh_host_key = models.TextField(blank=True)                      # "type base64": the SSH server's key, trusted when first connected to
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def database_name(self):
        """The database PGDAS logs into: the saved one, or postgres when none was given."""
        return (self.database or "").strip() or DEFAULT_DATABASE

    def set_password(self, raw):
        self.password_encrypted = crypto.encrypt(raw)

    def get_password(self):
        return crypto.decrypt(self.password_encrypted) if self.password_encrypted else ""

    def set_ssh_secret(self, raw):
        self.ssh_secret_encrypted = crypto.encrypt(raw) if raw else ""

    def get_ssh_secret(self):
        return crypto.decrypt(self.ssh_secret_encrypted) if self.ssh_secret_encrypted else ""

    def set_ssh_passphrase(self, raw):
        self.ssh_passphrase_encrypted = crypto.encrypt(raw) if raw else ""

    def get_ssh_passphrase(self):
        return crypto.decrypt(self.ssh_passphrase_encrypted) if self.ssh_passphrase_encrypted else ""
