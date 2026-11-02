"""What the dashboard shows, from the saved servers themselves (one short look at each), the audit log and the size samples."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Sum
from django.utils import timezone

from audit.models import AuditEntry
from explorer import pg
from servers.models import ServerConnection
from watch import alerts
from watch.models import SizeSample

TREND_DAYS = (1, 7, 30)


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


def _trend(days):
    """Total size of all servers over time: for each 10 minutes (24 hours), hour (7 days) or day (30 days) the latest known size of
    every server, added up."""
    since = timezone.now() - timedelta(days=days)
    minutes = 10 if days <= 1 else 60 if days <= 7 else 24 * 60
    latest = defaultdict(dict)                      # bucket -> server -> (when, total)
    for row in (SizeSample.objects.filter(taken_at__gte=since).order_by().values("server", "taken_at").annotate(total=Sum("size"))):
        taken = row["taken_at"]
        day_start = taken.replace(hour=0, minute=0, second=0, microsecond=0)
        key = day_start + timedelta(minutes=(taken - day_start).total_seconds() // 60 // minutes * minutes)
        held = latest[key].get(row["server"])
        if held is None or row["taken_at"] > held[0]:
            latest[key][row["server"]] = (row["taken_at"], row["total"])
    known, points = {}, []
    everyone = {s for per in latest.values() for s in per}
    for key in sorted(latest):
        for server, (_, total) in latest[key].items():
            known[server] = total
        if set(known) == everyone:                  # start once every server has a size, so the line does not climb as servers appear
            points.append({"t": key.isoformat(), "size": sum(known.values())})
    return points


def build(user, days=7):
    staff = user.is_staff
    servers = list(ServerConnection.objects.all())
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(servers)))) as pool:
        looks = list(pool.map(lambda s: _look(s, staff), servers))
    online = [s for s in looks if s["ok"]]
    databases = [(s["name"], d) for s in online for d in s["databases"]]
    total = sum(s["size"] for s in online)
    if staff:                                       # these read PGDAS's own SQLite data, so they run here, not in the threads
        for server, look in zip(servers, looks):
            if look["ok"]:
                look["alerts"] += alerts.growth(server) + alerts.backups(server)
    attention = [{**a, "server": s["name"]} for s in looks for a in s["alerts"]]
    users = get_user_model().objects.filter(is_active=True)
    busiest = max(online, key=lambda s: s["sessions"] / s["max"] if s["max"] else 0, default=None)
    data = {
        "is_staff": staff,
        "totals": {"servers": len(looks), "online": len(online), "offline": len(looks) - len(online), "users": users.count(),
                   "staff": users.filter(is_staff=True).count(), "databases": len(databases), "size": total, "size_text": pg.size_text(total),
                   "alerts": len(attention), "critical": sum(1 for a in attention if a["level"] == "critical")},
        "servers": [{k: v for k, v in s.items() if k not in ("databases", "alerts")} | {"databases": len(s["databases"]), "size_text": pg.size_text(s["size"])}
                    for s in looks],
        "gauges": {"connections": None if not busiest or not busiest["max"] else {
                       "percent": round(busiest["sessions"] * 100 / busiest["max"]), "label": f"{busiest['sessions']} of {busiest['max']}", "server": busiest["name"]},
                   "cache_hit": None if not online or all(s["cache_hit"] is None for s in online) else {
                       "percent": round(min(s["cache_hit"] for s in online if s["cache_hit"] is not None)), "label": "reads served from memory"},
                   "busy": None if not online or not sum(s["sessions"] for s in online) else {
                       "percent": round(sum(s["active"] for s in online) * 100 / sum(s["sessions"] for s in online)),
                       "label": f"{sum(s['active'] for s in online)} of {sum(s['sessions'] for s in online)} sessions running a query"}},
        "storage": [{"server": name, "database": d["name"], "size": d["size"], "size_text": d["size_text"]}
                    for name, d in sorted(databases, key=lambda x: -x[1]["size"])[:8]],
        "trend": _trend(days if days in TREND_DAYS else 7), "days": days if days in TREND_DAYS else 7,
        "activity": [], "attention": [],
    }
    if staff:
        data["activity"] = [{"when": e.created_at.isoformat(), "user": e.username, "server": e.server_name, "action": e.action,
                             "detail": (e.detail or "").splitlines()[0][:140] if e.detail else "", "ok": e.ok}
                            for e in AuditEntry.objects.all()[:8]]
        failed = AuditEntry.objects.filter(ok=False, created_at__gte=timezone.now() - timedelta(days=7))[:5]
        data["attention"] = ([{"level": a["level"], "title": f"{a['server']}: {a['title']}", "detail": a["detail"]} for a in attention] +
                             [{"level": "warning", "title": f"Failed {e.action} by {e.username}", "detail": (e.detail or "").splitlines()[0][:160] if e.detail else ""}
                              for e in failed])
    return data
