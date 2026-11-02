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
