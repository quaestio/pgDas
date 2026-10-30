import json

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
    # Structured record of a change, as JSON. For a delete this holds the database, table, statement and every deleted
    # row (see explorer.writes), which is what "Re-insert" puts back.
    payload = models.JSONField(null=True, blank=True)
    restored_at = models.DateTimeField(null=True, blank=True)
    restored_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        verbose_name_plural = "audit entries"

    def __str__(self):
        return f"{self.username} {self.action}"

    @property
    def is_delete_record(self):
        return bool(self.payload) and self.payload.get("kind") == "delete"

    @property
    def can_reinsert(self):
        return self.is_delete_record and self.ok and self.restored_at is None and bool(self.payload.get("rows"))

    @property
    def record_label(self):
        """One line saying what the JSON record is about, for the audit log."""
        p = self.payload or {}
        kind, where = p.get("kind"), f"{p.get('schema')}.{p.get('table')}"

        def rows(n):
            return f"{n} row{'' if n == 1 else 's'}"

        if kind == "delete":
            return f"{rows(p.get('count', 0))} from {where}"
        if kind == "update":
            return f"UPDATE · {rows(p.get('count', 0))} in {where}"
        if kind == "insert":
            return f"INSERT · {rows(p.get('count', 0))} into {where}"
        if kind == "restore":
            return f"re-insert · {rows(p.get('count', 0))} into {where}"
        if kind == "truncate":
            tables = p.get("tables", [])
            return f"TRUNCATE · {len(tables)} table{'' if len(tables) == 1 else 's'}, {rows(p.get('total_rows', 0))}"
        if kind == "drop":
            return f"DROP {p.get('object_type', '')}"
        if kind in ("create", "alter"):
            n = len(p.get("changes", []))
            return f"{kind.upper()} {p.get('object_type', '')} · {n} object{'' if n == 1 else 's'}" + (" · risky" if p.get("risky") else "")
        return ""

    @property
    def payload_preview(self):
        """The JSON shown in the audit log: the first few rows only; the whole record can be downloaded."""
        if not self.payload:
            return ""
        shown = {**self.payload, "rows": self.payload.get("rows", [])[:PREVIEW_ROWS]}
        text = json.dumps(shown, indent=2, default=str, ensure_ascii=False)
        more = len(self.payload.get("rows", [])) - PREVIEW_ROWS
        return text + (f"\n… {more} more row{'s' if more != 1 else ''} in the downloadable JSON" if more > 0 else "")


MAX_DETAIL = 5000
PREVIEW_ROWS = 10


def record(user, action, server=None, detail="", ok=True, rows=None, payload=None):
    return AuditEntry.objects.create(
        user=user if getattr(user, "pk", None) else None,
        username=getattr(user, "username", "") or "anonymous",
        server=server, server_name=server.name if server else "",
        action=action, detail=(detail or "")[:MAX_DETAIL], ok=ok, rows=rows, payload=payload,
    )
