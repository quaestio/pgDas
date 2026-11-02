"""Size samples: one row per database each time `run_watch` runs, kept for WATCH_KEEP_DAYS."""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from explorer import pg
from servers.models import ServerConnection

from .models import SizeSample


def sample(server, now=None):
    """Record the size of every database this login can see on the server. Returns how many samples were saved."""
    now = now or timezone.now()
    with pg.connect(server) as conn:
        databases = pg.list_databases(conn)
    SizeSample.objects.bulk_create([SizeSample(server=server, database=d["name"], size=d["size"], taken_at=now) for d in databases])
    return len(databases)


def prune(now=None):
    cutoff = (now or timezone.now()) - timedelta(days=settings.WATCH_KEEP_DAYS)
    return SizeSample.objects.filter(taken_at__lt=cutoff).delete()[0]


def sample_all(now=None):
    """Sample every saved server. A server that cannot be reached is reported, not fatal. Returns [(server name, count or error)]."""
    results = []
    for server in ServerConnection.objects.all():
        try:
            results.append((server.name, sample(server, now)))
        except pg.PgError as error:
            results.append((server.name, f"failed: {error}"))
    prune(now)
    return results


def series(server, days):
    """Per database: the samples of the last `days` days, the latest size and the change over the period."""
    since = timezone.now() - timedelta(days=days)
    grouped = {}
    for s in SizeSample.objects.filter(server=server, taken_at__gte=since).order_by("taken_at"):
        grouped.setdefault(s.database, []).append((s.taken_at, s.size))
    rows = []
    for name, points in sorted(grouped.items()):
        first, last = points[0][1], points[-1][1]
        rows.append({"database": name, "size": last, "size_text": pg.size_text(last), "change": last - first,
                     "change_text": ("+" if last >= first else "−") + pg.size_text(abs(last - first)),
                     "percent": None if not first else round((last - first) * 100 / first, 1), "samples": len(points),
                     "from": points[0][0].isoformat(), "points": [[t.isoformat(), v] for t, v in points]})
    return rows
