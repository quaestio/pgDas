"""What a server is doing right now, from PostgreSQL's own statistics views. Read-only; ending a session is in views.py."""
from explorer import pg

QUERY_LIMIT = 400

SESSIONS = f"""
SELECT pid, usename, datname, application_name, client_addr::text, state, wait_event_type, wait_event,
       extract(epoch FROM now() - query_start), extract(epoch FROM now() - xact_start),
       extract(epoch FROM now() - backend_start), left(query, {QUERY_LIMIT})
FROM pg_stat_activity
WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
ORDER BY (state = 'active') DESC, query_start NULLS LAST"""

LOCKS = f"""
SELECT a.pid, a.usename, a.datname, left(a.query, {QUERY_LIMIT}), extract(epoch FROM now() - a.query_start),
       b.pid, b.usename, left(b.query, {QUERY_LIMIT}), b.state
FROM pg_stat_activity a
CROSS JOIN LATERAL unnest(pg_blocking_pids(a.pid)) AS blocker(pid)
JOIN pg_stat_activity b ON b.pid = blocker.pid
ORDER BY a.query_start"""

STATEMENTS = f"""
SELECT left(query, {QUERY_LIMIT}), calls, total_exec_time, mean_exec_time, max_exec_time, rows
FROM pg_stat_statements
WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
ORDER BY total_exec_time DESC LIMIT 15"""


def _seconds(value):
    return None if value is None else round(float(value), 1)


def snapshot(server, slow_after=5):
    """Sessions, who is blocking whom, the long-running queries, and (when the extension is there) the costliest statements."""
    with pg.connect(server) as conn:
        sessions = [{"pid": r[0], "user": r[1], "database": r[2], "application": r[3], "client": r[4], "state": r[5],
                     "wait": " / ".join(w for w in (r[6], r[7]) if w), "running": _seconds(r[8]), "in_transaction": _seconds(r[9]),
                     "connected": _seconds(r[10]), "query": (r[11] or "").strip()}
                    for r in conn.execute(SESSIONS).fetchall()]
        locks = [{"blocked_pid": r[0], "blocked_user": r[1], "database": r[2], "blocked_query": (r[3] or "").strip(), "waiting": _seconds(r[4]),
                  "blocking_pid": r[5], "blocking_user": r[6], "blocking_query": (r[7] or "").strip(), "blocking_state": r[8]}
                 for r in conn.execute(LOCKS).fetchall()]
        statements, statements_note = None, None
        if conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'").fetchone():
            try:
                statements = [{"query": (r[0] or "").strip(), "calls": r[1], "total": round(r[2] / 1000, 2), "mean": round(r[3], 1),
                               "max": round(r[4], 1), "rows": r[5]} for r in conn.execute(STATEMENTS).fetchall()]
            except pg.PgError as error:
                statements_note = str(error)
        else:
            statements_note = ("pg_stat_statements is not installed in this database, so there is no history of slow queries. "
                               "CREATE EXTENSION pg_stat_statements (and add it to shared_preload_libraries) to get one.")
    slow = [s for s in sessions if s["state"] == "active" and (s["running"] or 0) >= slow_after]
    slow.sort(key=lambda s: -(s["running"] or 0))
    idle_in_transaction = [s for s in sessions if s["state"].startswith("idle in transaction")]
    return {"sessions": sessions, "locks": locks, "slow": slow, "idle_in_transaction": idle_in_transaction,
            "statements": statements, "statements_note": statements_note, "slow_after": slow_after}
