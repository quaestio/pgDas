from django.db import models

from servers.models import ServerConnection


class SizeSample(models.Model):
    """The size of one database at one moment; `manage.py run_watch` takes one for every database of every saved server."""

    server = models.ForeignKey(ServerConnection, on_delete=models.CASCADE, related_name="size_samples")
    database = models.CharField(max_length=200)
    size = models.BigIntegerField()
    taken_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["taken_at"]
        indexes = [models.Index(fields=["server", "database", "taken_at"])]


class AlertState(models.Model):
    """One alert as `run_watch` has seen it, so a problem is announced once when it starts and once when it is over,
    not at every run. `notified_at` is empty until a message about it was delivered."""

    server = models.ForeignKey(ServerConnection, on_delete=models.CASCADE, related_name="alert_states")
    key = models.CharField(max_length=200)
    level = models.CharField(max_length=10)
    title = models.CharField(max_length=300)
    detail = models.TextField(blank=True)
    first_seen = models.DateTimeField()
    last_seen = models.DateTimeField()
    notified_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["server", "key"], name="one_state_per_alert")]
