from django.conf import settings
from django.db import models


class SavedQuery(models.Model):
    """A statement a person keeps under a name on one server, to run again later from the SQL runner. Personal: nobody else sees it."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_queries")
    server = models.ForeignKey("servers.ServerConnection", on_delete=models.CASCADE, related_name="saved_queries")
    name = models.CharField(max_length=80)
    sql = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["user", "server", "name"], name="saved_query_name_once")]

    def __str__(self):
        return self.name
