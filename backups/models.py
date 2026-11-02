from pathlib import Path

from django.conf import settings
from django.db import models

from servers.models import ServerConnection


class Backup(models.Model):
    """One pg_dump file (custom format, restorable with pg_restore) of one database."""

    STATUSES = [("running", "running"), ("ok", "ok"), ("failed", "failed")]

    server = models.ForeignKey(ServerConnection, null=True, on_delete=models.SET_NULL, related_name="backups")
    server_name = models.CharField(max_length=100)
    database = models.CharField(max_length=200)
    kind = models.CharField(max_length=10, default="manual")          # manual or scheduled
    status = models.CharField(max_length=10, choices=STATUSES, default="running")
    filename = models.CharField(max_length=300, blank=True)
    size = models.BigIntegerField(default=0)
    error = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    @property
    def path(self):
        return Path(settings.BACKUP_DIR) / self.filename if self.filename else None

    @property
    def available(self):
        return self.status == "ok" and bool(self.path) and self.path.is_file()


class BackupSchedule(models.Model):
    """Back up one database every `every_hours` hours (run `manage.py run_backups` regularly), keeping the newest `keep`."""

    server = models.ForeignKey(ServerConnection, on_delete=models.CASCADE, related_name="backup_schedules")
    database = models.CharField(max_length=200)
    every_hours = models.PositiveIntegerField(default=24)
    keep = models.PositiveIntegerField(default=7)
    enabled = models.BooleanField(default=True)
    last_run = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["server__name", "database"]
        constraints = [models.UniqueConstraint(fields=["server", "database"], name="one_schedule_per_database")]
