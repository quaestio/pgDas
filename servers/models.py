from django.conf import settings
from django.db import models

from . import crypto


class ServerConnection(models.Model):
    SSL_MODES = [(mode, mode) for mode in ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")]

    name = models.CharField(max_length=100, unique=True)
    host = models.CharField(max_length=255)
    port = models.PositiveIntegerField(default=5432)
    database = models.CharField(max_length=100, default="postgres")
    username = models.CharField(max_length=100)
    password_encrypted = models.TextField(blank=True)
    sslmode = models.CharField(max_length=20, choices=SSL_MODES, default="prefer")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def set_password(self, raw):
        self.password_encrypted = crypto.encrypt(raw)

    def get_password(self):
        return crypto.decrypt(self.password_encrypted) if self.password_encrypted else ""
