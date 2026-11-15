"""Collecting one sample of everything PostgreSQL reports about itself, with ONE connection and a handful of short, read-only statistics queries.

* Works from PostgreSQL 12 to 18: each group of statistics is chosen for the server's version (for example the checkpointer moved to its own
  view in 17, session statistics arrived in 14, pg_stat_io in 16), and a group the login may not read, or the server does not have, is skipped
  and listed under `skipped` instead of failing the whole sample.
* Counters are stored as PostgreSQL reports them (cumulative); rates are worked out when a chart is drawn (see series.py).
* Nothing is installed on the monitored server. Every query has a short time limit so monitoring can never pile load onto it.
"""
from decimal import Decimal

import psycopg
from django.conf import settings

from explorer import pg

# pg_stat_database columns summed over all databases and kept per database. (name, minimum server version)
DB_COUNTERS = [("xact_commit", 0), ("xact_rollback", 0), ("blks_read", 0), ("blks_hit", 0), ("tup_returned", 0), ("tup_fetched", 0),
               ("tup_inserted", 0), ("tup_updated", 0), ("tup_deleted", 0), ("conflicts", 0), ("temp_files", 0), ("temp_bytes", 0),
               ("deadlocks", 0), ("blk_read_time", 0), ("blk_write_time", 0), ("checksum_failures", 0),
               ("sessions", 140000), ("sessions_abandoned", 140000), ("sessions_fatal", 140000), ("sessions_killed", 140000),
               ("session_time", 140000), ("active_time", 140000), ("idle_in_transaction_time", 140000)]

# Settings that explain the charts, shown on the Dashboards page.
SETTINGS = ["max_connections", "shared_buffers", "work_mem", "maintenance_work_mem", "effective_cache_size", "max_wal_size", "min_wal_size",
            "checkpoint_timeout", "autovacuum", "autovacuum_freeze_max_age", "track_io_timing", "track_counts", "wal_level", "synchronous_commit",
            "random_page_cost", "max_worker_processes", "max_parallel_workers", "log_min_duration_statement", "shared_preload_libraries"]

ACTIVITY = """
SELECT count(*) FILTER (WHERE backend_type = 'client backend'),
       count(*) FILTER (WHERE backend_type = 'client backend' AND state = 'active'),
       count(*) FILTER (WHERE backend_type = 'client backend' AND state = 'idle'),
       count(*) FILTER (WHERE backend_type = 'client backend' AND state = 'idle in transaction'),
       count(*) FILTER (WHERE backend_type = 'client backend' AND state = 'idle in transaction (aborted)'),
       count(*) FILTER (WHERE backend_type = 'client backend' AND wait_event_type = 'Lock'),
       count(*) FILTER (WHERE backend_type = 'client backend' AND cardinality(pg_blocking_pids(pid)) > 0),
       coalesce(max(extract(epoch FROM now() - xact_start)) FILTER (WHERE backend_type = 'client backend'), 0),
       coalesce(max(extract(epoch FROM now() - query_start)) FILTER (WHERE backend_type = 'client backend' AND state = 'active'), 0),
       coalesce(max(extract(epoch FROM now() - state_change)) FILTER (WHERE backend_type = 'client backend' AND state LIKE 'idle in transaction%%'), 0),
       count(*) FILTER (WHERE backend_type = 'autovacuum worker'),
       count(*) FILTER (WHERE backend_type = 'walsender')
FROM pg_stat_activity WHERE pid <> pg_backend_pid()"""
ACTIVITY_KEYS = ["conn_total", "conn_active", "conn_idle", "conn_idle_tx", "conn_idle_tx_aborted", "conn_waiting_lock", "blocked", "oldest_xact_s",
                 "longest_query_s", "oldest_idle_tx_s", "autovacuum_workers", "walsenders"]

PER_DATABASE_ACTIVITY = ("SELECT datname, count(*), count(*) FILTER (WHERE state = 'active') FROM pg_stat_activity "
                         "WHERE backend_type = 'client backend' AND datname IS NOT NULL GROUP BY datname")


def _number(value):
    if isinstance(value, Decimal):
        value = float(value)
    return value


def _row(values, keys):
    return {key: _number(value) for key, value in zip(keys, values)}


def database_sql(version):
    """The pg_stat_database query for this server's version."""
    columns = [name for name, minimum in DB_COUNTERS if version >= minimum]
    select = ", ".join(f"coalesce(d.{name}, 0)" for name in columns)
    return (f"SELECT p.datname, d.numbackends, age(p.datfrozenxid), mxid_age(p.datminmxid), {select} "
            "FROM pg_database p LEFT JOIN pg_stat_database d ON d.datid = p.oid WHERE NOT p.datistemplate AND p.datallowconn ORDER BY p.datname"), columns


def checkpoint_sql(version):
    """Checkpoint and background-writer counters. PostgreSQL 17 moved the checkpointer into its own view."""
    if version >= 170000:
        return [("SELECT num_timed, num_requested, write_time, sync_time, buffers_written FROM pg_stat_checkpointer",
                 ["checkpoints_timed", "checkpoints_req", "checkpoint_write_time", "checkpoint_sync_time", "buffers_checkpoint"]),
                ("SELECT buffers_clean, maxwritten_clean, buffers_alloc FROM pg_stat_bgwriter", ["buffers_clean", "maxwritten_clean", "buffers_alloc"])]
    keys = ["checkpoints_timed", "checkpoints_req", "checkpoint_write_time", "checkpoint_sync_time", "buffers_checkpoint", "buffers_clean",
            "maxwritten_clean", "buffers_backend", "buffers_backend_fsync", "buffers_alloc"]
    return [(f"SELECT {', '.join(keys)} FROM pg_stat_bgwriter", keys)]


def statements_sql(version, schema):
    total = "total_exec_time" if version >= 130000 else "total_time"
    return (f"SELECT queryid::text, left(query, 200), calls, {total}, rows, shared_blks_hit, shared_blks_read FROM {schema}.pg_stat_statements "
            f"WHERE dbid IS NOT NULL ORDER BY {total} DESC LIMIT 20")


def collect(server):
    """One sample: {"server": {...}, "databases": {name: {...}}, "meta": {...}}. Raises pg.PgError when the server cannot be reached."""
    skipped = []
    with pg.connect(server) as conn:
        conn.execute("SELECT set_config('statement_timeout', %s, false), set_config('application_name', 'PGDAS monitor', false)",
                     (str(int(settings.WATCH_SAMPLE_TIMEOUT_MS)), ))
        version, version_text, recovery, started = conn.execute(
            "SELECT current_setting('server_version_num')::int, version(), pg_is_in_recovery(), pg_postmaster_start_time()").fetchone()
        data = {"server": {}, "databases": {}, "meta": {"version": version, "version_text": " ".join(version_text.split()[:2]), "in_recovery": recovery,
                                                         "started_at": started.isoformat(), "skipped": skipped}}
        server_data, databases = data["server"], data["databases"]

        def group(name, work):
            """Run one group of statistics queries; a refusal (privilege, missing view) is noted and the rest of the sample carries on."""
            try:
                with conn.transaction():
                    work()
            except psycopg.Error as error:
                skipped.append(f"{name}: {pg._message(error)[:120]}")

        def activity():
            server_data.update(_row(conn.execute(ACTIVITY).fetchone(), ACTIVITY_KEYS))
            for name, total, active in conn.execute(PER_DATABASE_ACTIVITY).fetchall():
                databases.setdefault(name, {}).update({"conn_total": total, "conn_active": active})

        def database_stats():
            sql_text, columns = database_sql(version)
            sums = {name: 0 for name in columns}
            for row in conn.execute(sql_text).fetchall():
                name, backends, xid_age, mxid_age = row[0], row[1], row[2], row[3]
                stats = {column: _number(value) for column, value in zip(columns, row[4:])}
                databases.setdefault(name, {}).update({"numbackends": backends or 0, "xid_age": xid_age, "mxid_age": mxid_age, **stats})
                for column in columns:
                    sums[column] += stats[column] or 0
            server_data.update(sums)

        def sizes():
            # pg_database_size() walks the files of every database, which is slow on a big cluster: its own group, with a longer limit,
            # so a slow size never costs the rest of the sample.
            conn.execute("SELECT set_config('statement_timeout', %s, true)", (str(int(settings.WATCH_SAMPLE_TIMEOUT_MS) * 4), ))
            total = 0
            for name, size in conn.execute("SELECT datname, pg_database_size(datname) FROM pg_database WHERE NOT datistemplate AND datallowconn").fetchall():
                databases.setdefault(name, {})["size"] = size
                total += size or 0
            server_data["size_total"] = total

        def settings_group():
            names = ", ".join(f"current_setting('{n}', true)" for n in SETTINGS)
            values = conn.execute(f"SELECT {names}").fetchone()
            data["meta"]["settings"] = {n: v for n, v in zip(SETTINGS, values) if v is not None}
            limits = conn.execute("SELECT pg_size_bytes(current_setting('shared_buffers')), current_setting('max_connections')::int, "
                                  "current_setting('autovacuum_freeze_max_age')::bigint").fetchone()
            server_data.update({"shared_buffers_bytes": limits[0], "conn_max": limits[1], "freeze_max_age": limits[2]})
            reserved = conn.execute("SELECT coalesce(current_setting(%s, true), '0')::int", ("reserved_connections" if version >= 160000 else "superuser_reserved_connections",)).fetchone()[0]
            server_data["conn_reserved"] = reserved

        def background():
            for sql_text, keys in checkpoint_sql(version):
                server_data.update(_row(conn.execute(sql_text).fetchone(), keys))

        def wal():
            if recovery:
                row = conn.execute("SELECT pg_wal_lsn_diff(pg_last_wal_replay_lsn(), '0/0')::float8, pg_wal_lsn_diff(pg_last_wal_receive_lsn(), '0/0')::float8, "
                                   "extract(epoch FROM now() - pg_last_xact_replay_timestamp())").fetchone()
                server_data.update(_row(row, ["wal_replay_lsn", "wal_receive_lsn", "replay_delay_s"]))
            else:
                server_data["wal_lsn"] = _number(conn.execute("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::float8").fetchone()[0])
            if version >= 140000:
                server_data.update(_row(conn.execute("SELECT wal_records, wal_fpi, wal_bytes, wal_buffers_full FROM pg_stat_wal").fetchone(),
                                        ["wal_records", "wal_fpi", "wal_bytes", "wal_buffers_full"]))

        def wal_directory():
            size, files = conn.execute("SELECT coalesce(sum(size), 0), count(*) FROM pg_ls_waldir()").fetchone()
            server_data.update({"wal_dir_bytes": _number(size), "wal_dir_files": files})

        def replication():
            if recovery:
                row = conn.execute("SELECT status, coalesce(sender_host, '') FROM pg_stat_wal_receiver").fetchone()
                data["meta"]["receiver"] = {"status": row[0], "sender": row[1]} if row else {"status": "not receiving", "sender": ""}
                return
            keys = ["name", "client", "state", "sync", "sent_lag", "write_lag", "flush_lag", "replay_lag", "write_lag_s", "flush_lag_s", "replay_lag_s"]
            rows = conn.execute(
                "SELECT coalesce(application_name, ''), coalesce(client_addr::text, ''), coalesce(state, ''), coalesce(sync_state, ''), "
                "pg_wal_lsn_diff(pg_current_wal_lsn(), sent_lsn), pg_wal_lsn_diff(pg_current_wal_lsn(), write_lsn), "
                "pg_wal_lsn_diff(pg_current_wal_lsn(), flush_lsn), pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn), "
                "extract(epoch FROM write_lag), extract(epoch FROM flush_lag), extract(epoch FROM replay_lag) FROM pg_stat_replication ORDER BY 1, 2").fetchall()
            server_data["standbys"] = [_row(r, keys) for r in rows]

        def slots():
            extra = ", wal_status, safe_wal_size" if version >= 130000 else ""
            reference = "pg_last_wal_replay_lsn()" if recovery else "pg_current_wal_lsn()"
            rows = conn.execute(f"SELECT slot_name, slot_type, active, pg_wal_lsn_diff({reference}, restart_lsn){extra} FROM pg_replication_slots ORDER BY 1").fetchall()
            server_data["slots"] = [{"name": r[0], "type": r[1], "active": r[2], "retained": _number(r[3]), **({"wal_status": r[4], "safe": _number(r[5])} if version >= 130000 else {})}
                                    for r in rows]

        def other():
            prepared = conn.execute("SELECT count(*) FROM pg_prepared_xacts").fetchone()[0]
            locks, ungranted = conn.execute("SELECT count(*), count(*) FILTER (WHERE NOT granted) FROM pg_locks").fetchone()
            server_data.update({"prepared_xacts": prepared, "locks": locks, "locks_waiting": ungranted})

        def io():
            row = conn.execute("SELECT coalesce(sum(reads), 0), coalesce(sum(writes), 0), coalesce(sum(hits), 0), coalesce(sum(evictions), 0), "
                               "coalesce(sum(extends), 0), coalesce(sum(fsyncs), 0), coalesce(sum(read_time), 0), coalesce(sum(write_time), 0) FROM pg_stat_io").fetchone()
            server_data.update(_row(row, ["io_reads", "io_writes", "io_hits", "io_evictions", "io_extends", "io_fsyncs", "io_read_time", "io_write_time"]))

        def statements():
            found = conn.execute("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'pg_stat_statements'").fetchone()
            data["meta"]["pg_stat_statements"] = bool(found)
            if not found:
                return
            schema = '"' + found[0].replace('"', '""') + '"'
            rows = conn.execute(statements_sql(version, schema)).fetchall()
            server_data["statements"] = [{"id": r[0], "query": (r[1] or "").strip(), "calls": r[2], "total": _number(r[3]), "rows": r[4], "hit": r[5], "read": r[6]} for r in rows]
            if version >= 140000:
                server_data["statements_dealloc"] = conn.execute(f"SELECT dealloc FROM {schema}.pg_stat_statements_info").fetchone()[0]

        group("sessions", activity)
        group("databases", database_stats)
        group("sizes", sizes)
        group("settings", settings_group)
        group("checkpoints", background)
        group("wal", wal)
        group("wal directory", wal_directory)
        group("replication", replication)
        group("slots", slots)
        group("locks", other)
        if version >= 160000:
            group("io", io)
        group("statements", statements)
        conn.rollback()
    return data