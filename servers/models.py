from django.conf import settings
from django.db import models

from . import crypto


DEFAULT_DATABASE = "postgres"


class ServerConnection(models.Model):
    SSL_MODES = [(mode, mode) for mode in ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")]

    name = models.CharField(max_length=100, unique=True)
    host = models.CharField(max_length=255)
    port = models.PositiveIntegerField(default=5432)
    # Not on the Add/Edit form. PostgreSQL needs some database to log into, so PgDeck uses this one first (older entries
    # may have one saved) and otherwise falls back to postgres and friends; see explorer.pg._connect_first_usable.
    database = models.CharField(max_length=100, blank=True, default=DEFAULT_DATABASE)
    username = models.CharField(max_length=100)
    password_encrypted = models.TextField(blank=True)
    sslmode = models.CharField(max_length=20, choices=SSL_MODES, default="prefer")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def database_name(self):
        """The database PgDeck logs into: the saved one, or postgres when none was given."""
        return (self.database or "").strip() or DEFAULT_DATABASE

    def set_password(self, raw):
        self.password_encrypted = crypto.encrypt(raw)

    def get_password(self):
        return crypto.decrypt(self.password_encrypted) if self.password_encrypted else ""
