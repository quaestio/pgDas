"""Sampling every saved server, built to hold up with many servers of many organizations.

* One run (`manage.py run_watch`, from cron every minute or with --loop) samples only the servers that are DUE; each server is sampled
  every WATCH_INTERVAL_SECONDS, spread out by a small per-server offset so they do not all arrive at once.
* Servers are sampled in parallel (WATCH_WORKERS at a time) and every query on a monitored server has a short time limit, so one slow or
  dead server never holds up the others, and monitoring never piles load onto a database.
* A server that cannot be reached is tried less and less often (doubling up to WATCH_MAX_BACKOFF_SECONDS) instead of every interval.
* A lease keeps two runs from overlapping (a cron run that starts while the last one is still going just exits).
* A server whose password or SSH key is locked is skipped (there is nothing to try), and an organization can be switched off.
* Old samples are deleted in small batches so a prune never holds the database for long.
* Size samples (one row per database, used by the growth alerts and the old charts) are still written, from the same sample.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from explorer import pg
from servers.models import ServerConnection

from . import metrics
from .models import MetricSample, ServerHealth, SizeSample, WatchRun

STATEMENT_EVERY = 3                   # the (larger) list of top statements is kept with every third sample


def sample(server, now=None):
    """Sample one server now and store it. Returns how many databases it has. Raises pg.PgError when it cannot be reached."""
    now = now or timezone.now()
    data = metrics.collect(server)
    return store(server, data, now)


def store(server, data, now):
    health, _ = ServerHealth.objects.get_or_create(server=server)
    keep_statements = health.sample_count % STATEMENT_EVERY == 0
    server_data = dict(data["server"])
    if not keep_statements:
        server_data.pop("statements", None)
    rows = [MetricSample(server=server, database="", taken_at=now, data=server_data)]
    sizes = []
    for name, values in data["databases"].items():
        rows.append(MetricSample(server=server, database=name, taken_at=now, data=values))
        if values.get("size") is not None:
            sizes.append(SizeSample(server=server, database=name, size=values["size"], taken_at=now))
    with transaction.atomic():
        MetricSample.objects.bulk_create(rows)
        SizeSample.objects.bulk_create(sizes)
        meta = data["meta"]
        health.version_num = meta["version"]
        health.version_text = meta["version_text"]
        health.in_recovery = meta["in_recovery"]
        health.started_at = meta["started_at"]
        health.capabilities = {"skipped": meta["skipped"], "pg_stat_statements": meta.get("pg_stat_statements", False), "receiver": meta.get("receiver")}
        health.settings = meta.get("settings", {})
        health.last_ok_at = health.last_attempt_at = now
        health.failures, health.last_error = 0, ""
        health.sample_count += 1
        health.next_due = now + timedelta(seconds=_interval(server))
        health.save()
    return len(sizes)


def _interval(server):
    base = int(settings.WATCH_INTERVAL_SECONDS)
    return base + (server.pk * 7) % max(1, min(30, base // 10))          # a small per-server offset: servers do not all come due together


def _failed(server, error, now):
    health, _ = ServerHealth.objects.get_or_create(server=server)
    health.failures += 1
    health.last_attempt_at = now
    health.last_error = str(error)[:500]
    wait = min(int(settings.WATCH_INTERVAL_SECONDS) * (2 ** min(health.failures, 10)), int(settings.WATCH_MAX_BACKOFF_SECONDS))
    health.next_due = now + timedelta(seconds=wait)
    health.save()


def due_servers(now=None, force=False):
    """(servers to sample now, [(name, reason) skipped]). Organizations with monitoring off are left out; locked servers are skipped."""
    now = now or timezone.now()
    servers = ServerConnection.objects.exclude(organization_scope__organization__monitoring_enabled=False).select_related("health").order_by("pk")
    due, skipped = [], []
    for server in servers:
        health = getattr(server, "health", None)
        if not force and health is not None and health.next_due and health.next_due > now:
            continue
        if server.asks_for_secret and not server.unlocked_until:
            skipped.append((server.name, "locked: its password or SSH key passphrase is not saved"))
            continue
        due.append(server)
    return due, skipped


def _collect_one(server, threaded):
    try:
        return server, metrics.collect(server), None
    except pg.PgError as error:
        return server, None, str(error)
    except Exception as error:                                           # a bug or an odd answer must not stop the other servers
        return server, None, f"{type(error).__name__}: {error}"
    finally:
        if threaded:
            connection.close()                                           # each worker thread has its own database connection


def _begin(now):
    """Claim the run: None when another run is still going."""
    from access_control.models import Installation
    with transaction.atomic():
        Installation.objects.select_for_update().filter(pk=1).first()
        if WatchRun.objects.filter(finished_at__isnull=True, started_at__gte=now - timedelta(seconds=int(settings.WATCH_RUN_LEASE_SECONDS))).exists():
            return None
        return WatchRun.objects.create(started_at=now)


def run_due(now=None, force=False, workers=None):
    """Sample every server that is due. Returns [(server name, outcome)], or None when another run is in progress."""
    now = now or timezone.now()
    run = _begin(now)
    if run is None:
        return None
    started = time.monotonic()
    due, skipped = due_servers(now, force)
    results = [(name, f"skipped: {reason}") for name, reason in skipped]
    workers = max(1, int(workers or settings.WATCH_WORKERS))
    threaded = workers > 1 and len(due) > 1 and connection.vendor == "postgresql"
    try:
        if threaded:
            with ThreadPoolExecutor(max_workers=min(workers, len(due))) as pool:
                outcomes = list(pool.map(lambda s: _collect_one(s, True), due))
        else:
            outcomes = [_collect_one(s, False) for s in due]
        sampled = failed = 0
        for server, data, error in outcomes:                              # all writes happen here, in one thread
            if error:
                failed += 1
                _failed(server, error, now)
                results.append((server.name, f"failed: {error}"))
            else:
                sampled += 1
                results.append((server.name, store(server, data, now)))
        prune(now)
    finally:
        run.finished_at = timezone.now()
        run.due, run.skipped = len(due), len(skipped)
        run.sampled, run.failed = locals().get("sampled", 0), locals().get("failed", 0)
        run.seconds = round(time.monotonic() - started, 2)
        run.save()
    return results


def prune(now=None):
    """Delete samples past their retention, a few thousand rows at a time."""
    now = now or timezone.now()
    removed = 0
    for model, days in ((SizeSample, settings.WATCH_KEEP_DAYS), (MetricSample, settings.WATCH_METRIC_KEEP_DAYS)):
        cutoff = now - timedelta(days=days)
        while True:
            ids = list(model.objects.filter(taken_at__lt=cutoff).values_list("pk", flat=True)[:5000])
            if not ids:
                break
            removed += model.objects.filter(pk__in=ids).delete()[0]
    old_runs = list(WatchRun.objects.order_by("-started_at").values_list("pk", flat=True)[500:])
    if old_runs:
        WatchRun.objects.filter(pk__in=old_runs).delete()
    return removed


def sample_all(now=None):
    """Sample every saved server right now, due or not. A server that cannot be reached is reported, not fatal. Returns [(name, count or error)]."""
    results = run_due(now, force=True)
    return results if results is not None else []


def series(server, days):
    """Per database: the size samples of the last `days` days, the latest size and the change over the period."""
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