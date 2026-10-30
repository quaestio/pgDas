from django.conf import settings
from django.db import models


class AuditEntry(models.Model):
    """Append-only record of what users did. Names are copied so entries survive deleted users or servers."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    username = models.CharField(max_length=150)
    server = models.ForeignKey("servers.ServerConnection", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    server_name = models.CharField(max_length=100, blank=True)
    action = models.CharField(max_length=40)
    detail = models.TextField(blank=True)
    ok = models.BooleanField(default=True)
    rows = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        verbose_name_plural = "audit entries"

    def __str__(self):
        return f"{self.username} {self.action}"


MAX_DETAIL = 5000


def record(user, action, server=None, detail="", ok=True, rows=None):
    return AuditEntry.objects.create(
        user=user if getattr(user, "pk", None) else None,
        username=getattr(user, "username", "") or "anonymous",
        server=server, server_name=server.name if server else "",
        action=action, detail=(detail or "")[:MAX_DETAIL], ok=ok, rows=rows,
    )
