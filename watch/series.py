"""Turning stored samples into what the Dashboards page draws.

Counters are cumulative, so a chart point is the CHANGE between two neighbouring samples divided by the seconds between them. A restart or a
statistics reset makes a counter go backwards: that interval is left out (a gap) instead of drawing a huge wrong number. A long silence
(a server that was down) is also a gap. Long ranges are averaged into at most MAX_POINTS points so the page stays fast however much history there is.
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from . import server_settings
from .models import MetricSample, ServerHealth

RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600, "3d": 3 * 86400, "7d": 7 * 86400, "14d": 14 * 86400}
MAX_POINTS = 240
WRAPAROUND = 2 ** 31                       # transaction ids wrap at about 2.1 billion


def _samples(server, since, database):
    return [(t.timestamp(), data) for t, data in
            MetricSample.objects.filter(server=server, database=database, taken_at__gte=since).order_by("taken_at").values_list("taken_at", "data")]


def _gap(interval):
    return interval * 4                    # no point is drawn across a silence longer than this


def rate(samples, key, interval, scale=1.0):
    """Per-second change of a counter. [(time, value)]"""
    out, previous = [], None
    for t, data in samples:
        value = data.get(key)
        if value is not None and previous is not None:
            before_t, before = previous
            dt = t - before_t
            if 0 < dt <= _gap(interval) and value >= before:
                out.append((t, (value - before) / dt * scale))
        previous = (t, value) if value is not None else None
    return out


def ratio(samples, numerator, denominator, interval, percent=True):
    """Change of `numerator` over change of the sum of `denominator` keys, per interval. [(time, value)]"""
    out, previous = [], None
    for t, data in samples:
        if previous is not None and 0 < t - previous[0] <= _gap(interval):
            before = previous[1]
            try:
                top = sum(data[k] - before[k] for k in numerator)
                bottom = sum(data[k] - before[k] for k in denominator)
            except KeyError:
                top = bottom = None
            if bottom and bottom > 0 and top >= 0:
                out.append((t, top / bottom * (100 if percent else 1)))
        previous = (t, data)
    return out


def gauge(samples, key, scale=1.0):
    return [(t, data[key] * scale) for t, data in samples if data.get(key) is not None]


def derived(samples, function):
    out = []
    for t, data in samples:
        try:
            value = function(data)
        except (KeyError, TypeError, ZeroDivisionError):
            continue
        if value is not None:
            out.append((t, value))
    return out


def statements_rate(samples, interval):
    """Calls per second and average milliseconds per call, from the samples that carry the top-statement list."""
    calls, millis, previous = [], [], None
    for t, data in samples:
        current = data.get("statements")
        if current is None:
            continue
        if previous is not None:
            before_t, before = previous
            dt = t - before_t
            if 0 < dt <= _gap(interval) * 3:
                earlier = {s["id"]: s for s in before}
                dcalls = dtotal = 0
                for s in current:
                    b = earlier.get(s["id"])
                    if b is not None and s["calls"] >= b["calls"]:
                        dcalls += s["calls"] - b["calls"]
                        dtotal += max(0.0, s["total"] - b["total"])
                calls.append((t, dcalls / dt))
                if dcalls:
                    millis.append((t, dtotal / dcalls))
        previous = (t, current)
    return calls, millis


def compress(points, start, end, aggregate="avg"):
    """Average (or take the maximum of) the points of each bucket so a chart has at most MAX_POINTS points. [[unix seconds, value]]"""
    if not points:
        return []
    width = max((end - start) / MAX_POINTS, 1.0)
    buckets = {}
    for t, value in points:
        index = int((t - start) // width)
        entry = buckets.setdefault(index, [0.0, 0, value])
        entry[0] += value
        entry[1] += 1
        entry[2] = max(entry[2], value)
    return [[round(start + (index + 0.5) * width, 1), round(entry[2] if aggregate == "max" else entry[0] / entry[1], 4)]
            for index, entry in sorted(buckets.items())]


def _series(name, points, start, end, aggregate="avg", color=None):
    return {"name": name, "points": compress(points, start, end, aggregate), "color": color}


def _panel(title, unit, series, note="", stacked=False, minimum=None, maximum=None, description=""):
    series = [s for s in series if s["points"]]
    return {"title": title, "unit": unit, "series": series, "stacked": stacked, "note": note, "min": minimum, "max": maximum,
            "description": description, "empty": not series}


DATABASE_PANELS = ("tps", "rows", "cache", "db_connections", "states_time", "session_events", "trouble", "temp", "size", "io_wait", "xid")


def dashboard(server, hours, database="", restricted=False):
    """Everything the Dashboards page shows for one server (and optionally one database), for the last `hours` hours."""
    now = timezone.now()
    end, start = now.timestamp(), (now - timedelta(hours=hours)).timestamp()
    since = now - timedelta(hours=hours)
    health = ServerHealth.objects.filter(server=server).first()
    interval = int(settings.WATCH_INTERVAL_SECONDS)
    version = health.version_num if health else 0
    server_rows = _samples(server, since, "")
    db_rows = _samples(server, since, database) if database else server_rows
    scope = f"database {database}" if database else "all databases"

    def S(name, points, aggregate="avg", color=None):
        return _series(name, points, start, end, aggregate, color)

    def needs(minimum, text):
        return "" if version >= minimum else f"Needs PostgreSQL {text} or later (this server is {health.version_text if health else 'not sampled yet'})."

    panels = {}
    # ---- workload
    panels["tps"] = _panel(f"Transactions per second ({scope})", "per_s", [
        S("commits", rate(db_rows, "xact_commit", interval), color="#4cc38a"), S("rollbacks", rate(db_rows, "xact_rollback", interval), color="#ff6b6b")],
        description="Committed and rolled-back transactions.")
    calls, millis = statements_rate(server_rows, interval)
    note = "" if calls else "Needs the pg_stat_statements extension (see the Monitor page, Features). It covers the 20 costliest statements."
    panels["qps"] = _panel("Statements per second (top statements)", "per_s", [S("calls", calls, color="#5ec8ff")], note=note)
    panels["query_time"] = _panel("Average statement time (top statements)", "ms", [S("ms per call", millis, color="#ffd166")], note=note)
    panels["rows"] = _panel(f"Row activity ({scope})", "per_s", [
        S("returned", rate(db_rows, "tup_returned", interval), color="#5ec8ff"), S("fetched", rate(db_rows, "tup_fetched", interval), color="#9d8cff"),
        S("inserted", rate(db_rows, "tup_inserted", interval), color="#4cc38a"), S("updated", rate(db_rows, "tup_updated", interval), color="#ffd166"),
        S("deleted", rate(db_rows, "tup_deleted", interval), color="#ff6b6b")], description="Rows read from tables, and rows written.")
    panels["cache"] = _panel(f"Buffer cache hit and rollback ratio ({scope})", "percent", [
        S("cache hit %", ratio(db_rows, ["blks_hit"], ["blks_hit", "blks_read"], interval), color="#4cc38a"),
        S("rollback %", ratio(db_rows, ["xact_rollback"], ["xact_commit", "xact_rollback"], interval), color="#ff6b6b")], minimum=0, maximum=100)
    # ---- sessions
    panels["connections"] = _panel("Connections by state", "count", [
        S("active", gauge(server_rows, "conn_active"), color="#4cc38a"), S("idle", gauge(server_rows, "conn_idle"), color="#5ec8ff"),
        S("idle in transaction", gauge(server_rows, "conn_idle_tx"), color="#ffd166"), S("idle in transaction (aborted)", gauge(server_rows, "conn_idle_tx_aborted"), color="#ff6b6b")],
        stacked=True, description="Client sessions. The limit is max_connections.")
    panels["connection_use"] = _panel("Connection slots in use", "percent", [
        S("in use %", derived(server_rows, lambda d: d["conn_total"] * 100 / d["conn_max"]), "max", "#ffd166")], minimum=0, maximum=100)
    panels["waiting"] = _panel("Blocked sessions and waiting locks", "count", [
        S("blocked sessions", gauge(server_rows, "blocked"), "max", "#ff6b6b"), S("waiting on a lock", gauge(server_rows, "conn_waiting_lock"), "max", "#ffd166"),
        S("ungranted locks", gauge(server_rows, "locks_waiting"), "max", "#9d8cff")])
    panels["ages"] = _panel("Longest transaction, query and idle-in-transaction", "s", [
        S("oldest open transaction", gauge(server_rows, "oldest_xact_s"), "max", "#ffd166"), S("longest running query", gauge(server_rows, "longest_query_s"), "max", "#ff6b6b"),
        S("longest idle in transaction", gauge(server_rows, "oldest_idle_tx_s"), "max", "#9d8cff")])
    panels["states_time"] = _panel(f"Time the sessions spend working ({scope})", "ms_s", [
        S("active", rate(db_rows, "active_time", interval), color="#4cc38a"), S("idle in transaction", rate(db_rows, "idle_in_transaction_time", interval), color="#ffd166")],
        note=needs(140000, "14"), description="Milliseconds of session time per second.")
    panels["session_events"] = _panel(f"Sessions that ended badly ({scope})", "per_min", [
        S("abandoned", rate(db_rows, "sessions_abandoned", interval, 60), color="#ffd166"), S("fatal", rate(db_rows, "sessions_fatal", interval, 60), color="#ff6b6b"),
        S("killed", rate(db_rows, "sessions_killed", interval, 60), color="#9d8cff")], note=needs(140000, "14"))
    # ---- trouble
    panels["trouble"] = _panel(f"Deadlocks and conflicts ({scope})", "per_min", [
        S("deadlocks", rate(db_rows, "deadlocks", interval, 60), color="#ff6b6b"), S("recovery conflicts", rate(db_rows, "conflicts", interval, 60), color="#ffd166"),
        S("checksum failures", rate(db_rows, "checksum_failures", interval, 60), color="#9d8cff")])
    panels["temp"] = _panel(f"Temporary files ({scope})", "bytes_s", [S("bytes written", rate(db_rows, "temp_bytes", interval), color="#ffd166")],
                            description="Queries that spill to disk because work_mem is too small.")
    # ---- storage and WAL
    sizes = gauge(db_rows, "size") if database else gauge(server_rows, "size_total")
    panels["size"] = _panel(f"Size ({scope})", "bytes", [S("size", sizes, color="#5ec8ff")])
    wal_rate = rate(server_rows, "wal_bytes", interval) or rate(server_rows, "wal_lsn", interval) or rate(server_rows, "wal_replay_lsn", interval)
    panels["wal"] = _panel("WAL written per second", "bytes_s", [S("WAL", wal_rate, color="#9d8cff"), S("full-page images / s", rate(server_rows, "wal_fpi", interval), color="#ffd166")])
    panels["wal_dir"] = _panel("WAL on disk", "bytes", [S("pg_wal size", gauge(server_rows, "wal_dir_bytes"), color="#9d8cff")],
                               note="Needs the pg_monitor role (or a superuser) to read the WAL directory.")
    panels["checkpoints"] = _panel("Checkpoints", "per_min", [
        S("scheduled", rate(server_rows, "checkpoints_timed", interval, 60), color="#4cc38a"), S("requested (too much WAL)", rate(server_rows, "checkpoints_req", interval, 60), color="#ff6b6b")],
        description="Requested checkpoints mean max_wal_size is too small for the write load.")
    panels["buffers"] = _panel("Buffers written, by who", "per_s", [
        S("checkpointer", rate(server_rows, "buffers_checkpoint", interval), color="#4cc38a"), S("background writer", rate(server_rows, "buffers_clean", interval), color="#5ec8ff"),
        S("backends", rate(server_rows, "buffers_backend", interval), color="#ff6b6b"), S("allocated", rate(server_rows, "buffers_alloc", interval), color="#9d8cff")])
    panels["io_wait"] = _panel(f"Time waiting for disk ({scope})", "ms_s", [
        S("reads", rate(db_rows, "blk_read_time", interval), color="#5ec8ff"), S("writes", rate(db_rows, "blk_write_time", interval), color="#ffd166")],
        note="Needs track_io_timing = on on the server.")
    panels["io"] = _panel("I/O operations (pg_stat_io)", "per_s", [
        S("reads", rate(server_rows, "io_reads", interval), color="#5ec8ff"), S("writes", rate(server_rows, "io_writes", interval), color="#ffd166"),
        S("hits", rate(server_rows, "io_hits", interval), color="#4cc38a"), S("evictions", rate(server_rows, "io_evictions", interval), color="#ff6b6b")], note=needs(160000, "16"))
    # ---- maintenance and safety
    freeze = {}
    xid_names = [database] if database else sorted({d for d in _database_names(server, since)})[:8]
    for name in xid_names:
        rows = db_rows if database else _samples(server, since, name)
        freeze[name] = derived(rows, lambda d: d["xid_age"] * 100 / WRAPAROUND)
    panels["xid"] = _panel("Transaction ID age (percent of the wraparound limit)", "percent", [S(name, points, "max") for name, points in freeze.items()],
                           description="Autovacuum must freeze old rows before this reaches 100%. Above about 10% means vacuum is behind.", minimum=0)
    panels["autovacuum"] = _panel("Autovacuum workers and prepared transactions", "count", [
        S("autovacuum workers", gauge(server_rows, "autovacuum_workers"), "max", "#4cc38a"), S("prepared transactions", gauge(server_rows, "prepared_xacts"), "max", "#ff6b6b")])
    # ---- replication
    names = sorted({s["name"] or s["client"] for _, d in server_rows for s in d.get("standbys", [])})[:8]
    def standby(key):
        return [S(name, derived(server_rows, lambda d, n=name: next((x[key] for x in d["standbys"] if (x["name"] or x["client"]) == n), None)), "max") for name in names]
    panels["lag_bytes"] = _panel("Replication lag (bytes behind)", "bytes", standby("replay_lag"), note="" if names else "This server has no standbys connected.")
    panels["lag_seconds"] = _panel("Replication lag (seconds)", "s", standby("replay_lag_s") + [S("this standby: replay delay", gauge(server_rows, "replay_delay_s"), "max")],
                                   note="" if names or gauge(server_rows, "replay_delay_s") else "This server has no standbys connected.")
    slot_names = sorted({s["name"] for _, d in server_rows for s in d.get("slots", [])})[:8]
    panels["slots"] = _panel("WAL kept for replication slots", "bytes", [S(name, derived(server_rows, lambda d, n=name: next((x["retained"] for x in d["slots"] if x["name"] == n), None)), "max") for name in slot_names],
                             note="" if slot_names else "This server has no replication slots.", description="An inactive slot keeps WAL forever and can fill the disk.")
    for key, panel in panels.items():
        if panel["empty"] and not panel["note"]:
            panel["note"] = "No data in this period yet." if server_rows else "No samples yet: the sampler (manage.py run_watch) has not run for this server."
    panels["db_connections"] = _panel(f"Connections to {database or 'this database'}", "count", [
        S("connected", gauge(db_rows, "conn_total"), "max", "#5ec8ff"), S("active", gauge(db_rows, "conn_active"), "max", "#4cc38a")]) if database else _panel("", "count", [])
    if restricted:
        return restricted_view(server, since, panels, db_rows, interval, health, database, hours, start, end, server_rows)
    return {"panels": panels, "tiles": tiles(server, server_rows, db_rows, interval, health, database), "tables": tables(server, since, server_rows, interval, health),
            "range": {"hours": hours, "start": start, "end": end, "samples": len(server_rows), "interval": interval}}


def _database_names(server, since):
    return MetricSample.objects.filter(server=server, taken_at__gte=since).exclude(database="").order_by().values_list("database", flat=True).distinct()


def _last(points):
    return points[-1][1] if points else None


def tiles(server, server_rows, db_rows, interval, health, database):
    last = server_rows[-1][1] if server_rows else {}
    now = timezone.now().timestamp()
    tps = rate(db_rows, "xact_commit", interval)
    rollbacks = rate(db_rows, "xact_rollback", interval)
    total = (_last(tps) or 0) + (_last(rollbacks) or 0) if tps else None
    hit = ratio(db_rows, ["blks_hit"], ["blks_hit", "blks_read"], interval)
    wal = rate(server_rows, "wal_bytes", interval) or rate(server_rows, "wal_lsn", interval)
    started = health.started_at.timestamp() if health and health.started_at else None
    return {"connections": last.get("conn_total"), "max_connections": last.get("conn_max"), "active": last.get("conn_active"), "idle_in_transaction": last.get("conn_idle_tx"),
            "blocked": last.get("blocked"), "tps": total, "cache_hit": _last(hit), "wal_rate": _last(wal), "size": (db_rows[-1][1].get("size") if database and db_rows else last.get("size_total")),
            "uptime": (now - started) if started else None, "version": health.version_text if health else "", "version_num": health.version_num if health else 0,
            "role": ("standby" if health and health.in_recovery else "primary") if health and health.version_num else "", "standbys": len(last.get("standbys", [])),
            "slots": len(last.get("slots", [])), "oldest_transaction": last.get("oldest_xact_s"), "last_sample_age": (now - server_rows[-1][0]) if server_rows else None,
            "samples": health.sample_count if health else 0, "failures": health.failures if health else 0, "last_error": health.last_error if health else "",
            "skipped": (health.capabilities or {}).get("skipped", []) if health else [], "statements_extension": bool((health.capabilities or {}).get("pg_stat_statements")) if health else False}


def tables(server, since, server_rows, interval, health):
    """Top statements over the period, the standbys and slots now, one line per database, and the settings that explain the charts."""
    out = {"statements": [], "standbys": [], "slots": [], "databases": [], "settings": (health.settings if health else {}) or {},
           "settings_info": server_settings.describe(health.version_num if health else 0)}
    with_statements = [(t, d["statements"]) for t, d in server_rows if d.get("statements")]
    if len(with_statements) >= 2:
        first, last = with_statements[0][1], with_statements[-1][1]
        earlier = {s["id"]: s for s in first}
        rows = []
        for s in last:
            b = earlier.get(s["id"], {"calls": 0, "total": 0.0, "rows": 0, "hit": 0, "read": 0})
            calls, total = s["calls"] - b["calls"], s["total"] - b["total"]
            if calls > 0 and total >= 0:
                touched = (s["hit"] - b["hit"]) + (s["read"] - b["read"])
                rows.append({"query": s["query"], "calls": calls, "total_ms": round(total, 1), "mean_ms": round(total / calls, 2), "rows": s["rows"] - b["rows"],
                             "hit_percent": round((s["hit"] - b["hit"]) * 100 / touched, 1) if touched > 0 else None})
        out["statements"] = sorted(rows, key=lambda r: -r["total_ms"])[:15]
    if server_rows:
        last = server_rows[-1][1]
        out["standbys"] = last.get("standbys", [])
        out["slots"] = last.get("slots", [])
    latest = {}
    for name in _database_names(server, since):
        rows = _samples(server, since, name)
        if not rows:
            continue
        data = rows[-1][1]
        hit = ratio(rows, ["blks_hit"], ["blks_hit", "blks_read"], interval)
        tps = rate(rows, "xact_commit", interval)
        latest[name] = {"database": name, "size": data.get("size"), "connections": data.get("conn_total", 0), "active": data.get("conn_active", 0),
                        "tps": round(_last(tps), 2) if tps else None, "cache_hit": round(_last(hit), 1) if hit else None,
                        "deadlocks": int((rows[-1][1].get("deadlocks") or 0) - (rows[0][1].get("deadlocks") or 0)) if len(rows) > 1 else 0,
                        "xid_percent": round((data.get("xid_age") or 0) * 100 / WRAPAROUND, 2)}
    out["databases"] = sorted(latest.values(), key=lambda r: -(r["size"] or 0))
    return out

def restricted_view(server, since, panels, db_rows, interval, health, database, hours, start, end, server_rows):
    """What an employee may see about ONE database they can read: that database's own counters and nothing about the rest of the server (no total
    connections, other databases, WAL, replication, settings or statements, which describe the whole server and everybody on it)."""
    allowed = {key: panels[key] for key in DATABASE_PANELS if key in panels}
    for key, panel in allowed.items():
        panel["title"] = panel["title"].replace("(all databases)", f"({database})")
    for key in ("tps", "rows", "cache", "trouble", "temp", "io_wait", "states_time", "session_events", "db_connections", "size", "xid"):
        if key in allowed and allowed[key]["note"] == "" and allowed[key]["empty"]:
            allowed[key]["note"] = "No data in this period yet." if db_rows else "No samples yet: the sampler has not run for this server."
    hit = ratio(db_rows, ["blks_hit"], ["blks_hit", "blks_read"], interval)
    commits, rollbacks = rate(db_rows, "xact_commit", interval), rate(db_rows, "xact_rollback", interval)
    last = db_rows[-1][1] if db_rows else {}
    now = timezone.now().timestamp()
    tiles_out = {"connections": last.get("conn_total"), "active": last.get("conn_active"), "tps": ((_last(commits) or 0) + (_last(rollbacks) or 0)) if commits else None,
                 "cache_hit": _last(hit), "size": last.get("size"), "xid_percent": round((last.get("xid_age") or 0) * 100 / WRAPAROUND, 2) if last else None,
                 "last_sample_age": (now - db_rows[-1][0]) if db_rows else None, "version": health.version_text if health else "", "samples": health.sample_count if health else 0,
                 "failures": 0, "last_error": "", "skipped": [], "statements_extension": False}
    databases = []
    if db_rows:
        data = db_rows[-1][1]
        tps = rate(db_rows, "xact_commit", interval)
        databases = [{"database": database, "size": data.get("size"), "connections": data.get("conn_total", 0), "active": data.get("conn_active", 0),
                      "tps": round(_last(tps), 2) if tps else None, "cache_hit": round(_last(hit), 1) if hit else None,
                      "deadlocks": int((db_rows[-1][1].get("deadlocks") or 0) - (db_rows[0][1].get("deadlocks") or 0)) if len(db_rows) > 1 else 0,
                      "xid_percent": round((data.get("xid_age") or 0) * 100 / WRAPAROUND, 2)}]
    return {"scope": "database", "panels": allowed, "tiles": tiles_out,
            "tables": {"statements": [], "standbys": [], "slots": [], "databases": databases, "settings": {}},
            "range": {"hours": hours, "start": start, "end": end, "samples": len(db_rows), "interval": interval}}
