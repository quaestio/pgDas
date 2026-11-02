"""Alerts worked out when the page is opened: nothing is stored, nothing is sent. Each is a plain statement of what is wrong."""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from backups.models import Backup, BackupSchedule
from explorer import pg

from .models import SizeSample

CRITICAL, WARNING = "critical", "warning"


def _alert(level, title, detail=""):
    return {"level": level, "title": title, "detail": detail}


def _minutes(seconds):
    return f"{seconds / 60:.0f} min" if seconds < 5400 else f"{seconds / 3600:.1f} h"


def live(server):
    """Problems visible right now on the server itself."""
    found = []
    try:
        with pg.connect(server) as conn:
            blocked, waiting = conn.execute(
                "SELECT count(*), max(extract(epoch FROM now() - query_start)) FROM pg_stat_activity "
                "WHERE cardinality(pg_blocking_pids(pid)) > 0").fetchone()
            if blocked and (waiting or 0) >= settings.WATCH_BLOCKED_SECONDS:
                found.append(_alert(CRITICAL, f"{blocked} session{'s are' if blocked != 1 else ' is'} blocked by locks",
                                    f"The longest has waited {_minutes(waiting)}. Open Monitor to see who blocks whom."))
            idle, oldest = conn.execute(
                "SELECT count(*), max(extract(epoch FROM now() - xact_start)) FROM pg_stat_activity "
                "WHERE state LIKE 'idle in transaction%%' AND now() - xact_start > make_interval(secs => %s)",
                (settings.WATCH_IDLE_TX_SECONDS,)).fetchone()
            if idle:
                found.append(_alert(WARNING, f"{idle} session{'s have' if idle != 1 else ' has'} been idle in a transaction too long",
                                    f"The oldest for {_minutes(oldest)}. Such sessions hold locks and stop clean-up."))
            slow, longest = conn.execute(
                "SELECT count(*), max(extract(epoch FROM now() - query_start)) FROM pg_stat_activity "
                "WHERE state = 'active' AND backend_type = 'client backend' AND pid <> pg_backend_pid() "
                "AND now() - query_start > make_interval(secs => %s)", (settings.WATCH_SLOW_SECONDS,)).fetchone()
            if slow:
                found.append(_alert(WARNING, f"{slow} quer{'ies have' if slow != 1 else 'y has'} run for over {_minutes(settings.WATCH_SLOW_SECONDS)}",
                                    f"The longest for {_minutes(longest)}."))
            used, limit = conn.execute("SELECT count(*), current_setting('max_connections')::int FROM pg_stat_activity "
                                       "WHERE backend_type = 'client backend'").fetchone()
            if used * 100 >= limit * settings.WATCH_CONNECTIONS_PERCENT:
                found.append(_alert(CRITICAL if used >= limit - 3 else WARNING, f"{used} of {limit} connections are in use",
                                    "When all are taken, new connections are refused."))
    except pg.PgError as error:
        found.append(_alert(CRITICAL, "The server cannot be reached", str(error)))
    return found


def growth(server, now=None):
    """Databases that grew faster than WATCH_GROWTH_PERCENT over the last 7 days (and by a worthwhile amount)."""
    now = now or timezone.now()
    since = now - timedelta(days=7)
    found = []
    names = SizeSample.objects.filter(server=server, taken_at__gte=since).order_by().values_list("database", flat=True).distinct()
    for name in names:
        points = list(SizeSample.objects.filter(server=server, database=name, taken_at__gte=since).order_by("taken_at"))
        first, last = points[0], points[-1]
        if len(points) < 2 or last.taken_at - first.taken_at < timedelta(hours=12) or not first.size:
            continue                                           # too little history to call it a trend
        grew = last.size - first.size
        percent = grew * 100 / first.size
        if percent >= settings.WATCH_GROWTH_PERCENT and grew >= settings.WATCH_GROWTH_MIN_BYTES:
            found.append(_alert(WARNING, f"{name} grew by {percent:.0f}% in {(last.taken_at - first.taken_at).days or 1} day(s)",
                                f"From {pg.size_text(first.size)} to {pg.size_text(last.size)}."))
    return found


def backups(server, now=None):
    """Failed or overdue scheduled backups, and a failed latest manual backup."""
    now = now or timezone.now()
    found = []
    for schedule in BackupSchedule.objects.filter(server=server, enabled=True):
        latest = Backup.objects.filter(server=server, database=schedule.database, kind="scheduled").first()
        if latest and latest.status == "failed":
            found.append(_alert(CRITICAL, f"The last scheduled backup of {schedule.database} failed", latest.error[:300]))
        overdue_after = timedelta(hours=schedule.every_hours * 2)
        reference = schedule.last_run or schedule.created_at
        if now - reference > overdue_after:
            found.append(_alert(WARNING, f"The backup of {schedule.database} is overdue",
                                f"It should run every {schedule.every_hours} h but the last run was "
                                f"{'never' if not schedule.last_run else _minutes((now - schedule.last_run).total_seconds()) + ' ago'}. "
                                "Is `manage.py run_backups` still being run?"))
    return found


def evaluate(server):
    alerts = live(server)
    if alerts and alerts[0]["title"] == "The server cannot be reached":
        return alerts                                           # nothing else can be known about it
    return alerts + growth(server) + backups(server)
