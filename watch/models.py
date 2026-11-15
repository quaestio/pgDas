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

class MetricSample(models.Model):
    """What one server (database = "") or one of its databases looked like at one moment: gauges as they were, counters as PostgreSQL
    reports them (cumulative), so rates are worked out when a chart is drawn, and a restart or a statistics reset only skips one interval.
    One row per server plus one per database per sample, each a small JSON document."""

    server = models.ForeignKey(ServerConnection, on_delete=models.CASCADE, related_name="metric_samples")
    database = models.CharField(max_length=200, blank=True)
    taken_at = models.DateTimeField()
    data = models.JSONField(default=dict)

    class Meta:
        ordering = ["taken_at"]
        indexes = [models.Index(fields=["server", "database", "taken_at"])]


class ServerHealth(models.Model):
    """The sampler's memory of one server: when it is due next, how often it failed in a row (so a dead server is tried less and less
    often), and what the server is (version, role, which statistics it offers)."""

    server = models.OneToOneField(ServerConnection, on_delete=models.CASCADE, related_name="health")
    next_due = models.DateTimeField(null=True, blank=True, db_index=True)
    last_ok_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    failures = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    version_num = models.PositiveIntegerField(default=0)
    version_text = models.CharField(max_length=120, blank=True)
    in_recovery = models.BooleanField(default=False)
    started_at = models.DateTimeField(null=True, blank=True)
    capabilities = models.JSONField(default=dict, blank=True)        # which statistics views exist and what was skipped
    settings = models.JSONField(default=dict, blank=True)            # a few settings that explain the charts (shared_buffers, ...)
    sample_count = models.PositiveIntegerField(default=0)


class WatchRun(models.Model):
    """One run of the sampler: the lease that keeps two runs from overlapping, and the numbers for the Monitoring page."""

    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    due = models.PositiveIntegerField(default=0)
    sampled = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)
    seconds = models.FloatField(default=0)

    class Meta:
        ordering = ["-started_at"]
