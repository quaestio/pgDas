"""What the dashboard shows, from the saved servers themselves (one short look at each), the audit log and the size samples."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from access_control import runtime
from audit.models import AuditEntry
from explorer import pg
from servers.models import ServerConnection
from watch import alerts


def _look(server, with_alerts):
    """One server: whether it answers, its version, sessions, databases and sizes, and (for staff) its alerts."""
    info = {"id": server.pk, "name": server.name, "host": f"{server.host}:{server.port}", "ok": False, "error": "", "version": "",
            "sessions": 0, "active": 0, "max": 0, "cache_hit": None, "databases": [], "size": 0}
    try:
        with pg.connect(server) as conn:
            version, sessions, active, limit, hit = conn.execute(
                "SELECT version(), "
                "(SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()), "
                "(SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend' AND state = 'active' AND pid <> pg_backend_pid()), "
                "current_setting('max_connections')::int, "
                "(SELECT round(sum(blks_hit) * 100.0 / nullif(sum(blks_hit + blks_read), 0), 1) FROM pg_stat_database)").fetchone()
            info["databases"] = pg.list_databases(conn)
        info.update(ok=True, version=" ".join(version.split()[:2]), sessions=sessions, active=active, max=limit,
                    cache_hit=None if hit is None else float(hit), size=sum(d["size"] for d in info["databases"]))
    except pg.PgError as error:
        info["error"] = str(error)
    info["alerts"] = alerts.live(server) if with_alerts else []          # PostgreSQL only: this runs in a worker thread
    return info


def _urls(pk):
    """Where the buttons of a server row go (the page builds the rows, so it needs the addresses)."""
    return {"sql": reverse("sql_runner", args=[pk]), "test": reverse("server_test", args=[pk]),
            "edit": reverse("server_edit", args=[pk]), "remove": reverse("server_delete", args=[pk])}


def build(user):
    staff = user.is_staff
    servers = list(runtime.servers())
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(servers)))) as pool:
        looks = list(pool.map(lambda s: _look(s, staff), servers))
    online = [s for s in looks if s["ok"]]
    databases = [(s["name"], d) for s in online for d in s["databases"]]
    total = sum(s["size"] for s in online)
    if staff:                                       # these read PGDAS's own database, so they run here, not in the threads
        for server, look in zip(servers, looks):
            if look["ok"]:
                look["alerts"] += alerts.growth(server) + alerts.backups(server)
    attention = [{**a, "server": s["name"]} for s in looks for a in s["alerts"]]
    users = get_user_model().objects.filter(is_active=True, pgdas_membership__organization_id=runtime.membership_for(user).organization_id)
    busiest = max(online, key=lambda s: s["sessions"] / s["max"] if s["max"] else 0, default=None)
    data = {
        "is_staff": staff, "can_manage": staff,                # Edit and Remove on the server rows
        "totals": {"servers": len(looks), "online": len(online), "offline": len(looks) - len(online), "users": users.count(),
                   "staff": users.filter(is_staff=True).count(), "databases": len(databases), "size": total, "size_text": pg.size_text(total),
                   "alerts": len(attention), "critical": sum(1 for a in attention if a["level"] == "critical")},
        "servers": [{k: v for k, v in s.items() if k not in ("databases", "alerts")} | {"databases": len(s["databases"]), "size_text": pg.size_text(s["size"]),
                                                                       "urls": _urls(s["id"])}
                    for s in looks],
        "gauges": {"connections": None if not busiest or not busiest["max"] else {
                       "percent": round(busiest["sessions"] * 100 / busiest["max"]), "label": f"{busiest['sessions']} of {busiest['max']}", "server": busiest["name"]},
                   "cache_hit": None if not online or all(s["cache_hit"] is None for s in online) else {
                       "percent": round(min(s["cache_hit"] for s in online if s["cache_hit"] is not None)), "label": "reads served from memory"},
                   "busy": None if not online or not sum(s["sessions"] for s in online) else {
                       "percent": round(sum(s["active"] for s in online) * 100 / sum(s["sessions"] for s in online)),
                       "label": f"{sum(s['active'] for s in online)} of {sum(s['sessions'] for s in online)} running a query"}},
        "storage": [{"server": name, "database": d["name"], "size": d["size"], "size_text": d["size_text"]}
                    for name, d in sorted(databases, key=lambda x: -x[1]["size"])[:8]],
        "activity": [], "attention": [],
    }
    if staff:
        data["activity"] = [{"when": e.created_at.isoformat(), "user": e.username, "server": e.server_name, "action": e.action,
                             "detail": (e.detail or "").splitlines()[0][:140] if e.detail else "", "ok": e.ok}
                            for e in runtime.audits()[:8]]
        failed = runtime.audits().filter(ok=False, created_at__gte=timezone.now() - timedelta(days=7))[:5]
        data["attention"] = ([{"level": a["level"], "title": f"{a['server']}: {a['title']}", "detail": a["detail"]} for a in attention] +
                             [{"level": "warning", "title": f"Failed {e.action} by {e.username}", "detail": (e.detail or "").splitlines()[0][:160] if e.detail else ""}
                              for e in failed])
    return data
