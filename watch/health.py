"""Table and index health for one database, read on demand (not sampled: it needs a connection to that database).

One connection, a few read-only statistics queries with a time limit, at most `limit` tables and indexes (the biggest first). Everything is explained in plain
words with the SQL that would fix it; nothing is changed here (the page offers VACUUM / ANALYZE through the Maintenance permission, the rest is for the person
to run in the SQL runner). Version-aware: PostgreSQL 16 adds when an index or table was last used, 13 adds inserts since the last vacuum.
"""
from datetime import timezone as _tz
from decimal import Decimal

import psycopg
from django.conf import settings

from explorer import pg

DEAD_ROWS_MIN = 1000
DEAD_PERCENT_WARNING = 20
DEAD_PERCENT_CRITICAL = 50
STALE_ANALYZE_PERCENT = 10
FREEZE_WARNING = 0.5
FREEZE_CRITICAL = 0.8
UNUSED_INDEX_MIN_BYTES = 1024 * 1024
UNUSED_INDEX_MIN_DAYS = 7
BLOAT_SCAN_MAX_BYTES = 2 * 1024 ** 3


def quote(name):
    return '"' + str(name).replace('"', '""') + '"'


def table_sql(version):
    extra_tables = ", s.n_ins_since_vacuum" if version >= 130000 else ", 0"
    last_seen = ", s.last_seq_scan, s.last_idx_scan" if version >= 160000 else ", NULL::timestamptz, NULL::timestamptz"
    return f"""
SELECT s.schemaname, s.relname, pg_total_relation_size(c.oid), pg_relation_size(c.oid), pg_indexes_size(c.oid),
       coalesce(pg_total_relation_size(nullif(c.reltoastrelid, 0)), 0), s.n_live_tup, s.n_dead_tup, s.n_mod_since_analyze{extra_tables},
       s.last_vacuum, s.last_autovacuum, s.last_analyze, s.last_autoanalyze, s.vacuum_count, s.autovacuum_count, s.analyze_count, s.autoanalyze_count,
       s.seq_scan, s.seq_tup_read, coalesce(s.idx_scan, 0), coalesce(s.idx_tup_fetch, 0), s.n_tup_ins, s.n_tup_upd, s.n_tup_del, s.n_tup_hot_upd,
       age(c.relfrozenxid), mxid_age(c.relminmxid), coalesce(c.reloptions::text[], '{{}}'::text[]),
       EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid AND i.indisprimary), c.relkind,
       coalesce(io.heap_blks_read, 0), coalesce(io.heap_blks_hit, 0), coalesce(io.idx_blks_read, 0), coalesce(io.idx_blks_hit, 0){last_seen}
FROM pg_stat_user_tables s JOIN pg_class c ON c.oid = s.relid LEFT JOIN pg_statio_user_tables io ON io.relid = s.relid
ORDER BY pg_total_relation_size(c.oid) DESC LIMIT %s"""


TABLE_KEYS = ["schema", "table", "total", "heap", "indexes", "toast", "live", "dead", "mod_since_analyze", "ins_since_vacuum", "last_vacuum", "last_autovacuum",
              "last_analyze", "last_autoanalyze", "vacuum_count", "autovacuum_count", "analyze_count", "autoanalyze_count", "seq_scan", "seq_tup_read",
              "idx_scan", "idx_tup_fetch", "inserted", "updated", "deleted", "hot_updated", "xid_age", "mxid_age", "options", "has_primary_key", "kind",
              "heap_read", "heap_hit", "idx_read", "idx_hit", "last_seq_scan", "last_idx_scan"]


def index_sql(version):
    last = ", s.last_idx_scan" if version >= 160000 else ", NULL::timestamptz"
    return f"""
SELECT s.schemaname, s.relname, s.indexrelname, i.indisunique, i.indisprimary, i.indisvalid, pg_relation_size(s.indexrelid), coalesce(s.idx_scan, 0),
       coalesce(s.idx_tup_read, 0), coalesce(s.idx_tup_fetch, 0), pg_get_indexdef(s.indexrelid), i.indrelid::int8,
       i.indkey::text, i.indclass::text, i.indoption::text, coalesce(pg_get_expr(i.indpred, i.indrelid), ''), coalesce(pg_get_expr(i.indexprs, i.indrelid), ''),
       (SELECT count(*) FROM pg_constraint k WHERE k.conindid = s.indexrelid){last}
FROM pg_stat_user_indexes s JOIN pg_index i ON i.indexrelid = s.indexrelid ORDER BY pg_relation_size(s.indexrelid) DESC LIMIT %s"""


INDEX_KEYS = ["schema", "table", "index", "unique", "primary", "valid", "size", "scans", "tup_read", "tup_fetch", "definition", "table_oid", "key", "opclass", "options",
              "predicate", "expressions", "constraints", "last_scan"]


def _clean(value):
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return (value if value.tzinfo else value.replace(tzinfo=_tz.utc)).isoformat()
    return value


def _rows(cursor, keys):
    return [{key: _clean(value) for key, value in zip(keys, row)} for row in cursor.fetchall()]


def collect(server, database, limit=150):
    """Everything the Health page shows for one database: tables, indexes, and the numbers that frame them. Raises pg.PgError."""
    limit = max(10, min(int(limit), 500))
    with pg.connect(server, database) as conn:
        conn.execute("SELECT set_config('statement_timeout', %s, false), set_config('application_name', 'PGDAS health', false)",
                     (str(int(settings.WATCH_SAMPLE_TIMEOUT_MS) * 3), ))
        version, freeze_max, started, reset = conn.execute(
            "SELECT current_setting('server_version_num')::int, current_setting('autovacuum_freeze_max_age')::bigint, pg_postmaster_start_time(), "
            "(SELECT stats_reset FROM pg_stat_database WHERE datname = current_database())").fetchone()
        notes = []
        try:
            with conn.transaction():
                tables = _rows(conn.execute(table_sql(version), (limit,)), TABLE_KEYS + ([] if True else []))
        except psycopg.Error as error:
            tables = []
            notes.append(f"tables: {pg._message(error)[:150]}")
        try:
            with conn.transaction():
                indexes = _rows(conn.execute(index_sql(version), (limit * 2,)), INDEX_KEYS)
        except psycopg.Error as error:
            indexes = []
            notes.append(f"indexes: {pg._message(error)[:150]}")
        try:
            with conn.transaction():
                running = [{"table": r[0], "phase": r[1], "heap_total": r[2], "heap_scanned": r[3]} for r in conn.execute(
                    "SELECT c.relname, p.phase, p.heap_blks_total, p.heap_blks_scanned FROM pg_stat_progress_vacuum p JOIN pg_class c ON c.oid = p.relid "
                    "WHERE p.datid = (SELECT oid FROM pg_database WHERE datname = current_database())").fetchall()]
        except psycopg.Error:
            running = []
        bloat_tool = bool(conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'pgstattuple'").fetchone())
        total_tables = conn.execute("SELECT count(*) FROM pg_stat_user_tables").fetchone()[0]
        total_indexes = conn.execute("SELECT count(*) FROM pg_stat_user_indexes").fetchone()[0]
        conn.rollback()
    import time
    stats_reset = _clean(reset)
    stats_age_days = None
    if reset is not None:
        from django.utils import timezone
        stats_age_days = round((timezone.now() - (reset if reset.tzinfo else reset.replace(tzinfo=_tz.utc))).total_seconds() / 86400, 1)
    data = {"database": database, "version": version, "freeze_max_age": freeze_max, "limit": limit, "tables": tables, "indexes": indexes, "running_vacuums": running,
            "pgstattuple": bloat_tool, "table_count": total_tables, "index_count": total_indexes, "stats_reset": stats_reset, "stats_age_days": stats_age_days,
            "taken": time.time(), "notes": notes, "truncated": total_tables > len(tables) or total_indexes > len(indexes)}
    data["findings"] = findings(data)
    data["summary"] = summary(data)
    return data


def _age_days(timestamp):
    from datetime import datetime
    from django.utils import timezone
    if not timestamp:
        return None
    moment = datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else timestamp
    return (timezone.now() - moment).total_seconds() / 86400


def _name(row, key="table"):
    return f"{row['schema']}.{row[key]}"


def findings(data):
    """Ranked problems and suggestions, each with the reason in plain words and the SQL that would deal with it."""
    out = []
    freeze_max = data["freeze_max_age"] or 200_000_000
    for t in data["tables"]:
        name, ref = _name(t), f"{quote(t['schema'])}.{quote(t['table'])}"
        live, dead = t["live"] or 0, t["dead"] or 0
        percent = dead * 100 / (live + dead) if live + dead else 0
        if dead >= DEAD_ROWS_MIN and percent >= DEAD_PERCENT_WARNING:
            level = "critical" if percent >= DEAD_PERCENT_CRITICAL and dead >= 100_000 else "warning"
            out.append({"level": level, "kind": "dead_rows", "object": name, "title": f"{dead:,} dead rows ({percent:.0f}% of the table)",
                        "detail": "Rows that were updated or deleted but not cleaned up yet. They make the table and its indexes bigger and queries slower. "
                                  "Autovacuum is behind (or switched off for this table).", "sql": f"VACUUM (ANALYZE) {ref};", "table": [t["schema"], t["table"]]})
        modified = t["mod_since_analyze"] or 0
        last_analyze = max(filter(None, [t["last_analyze"], t["last_autoanalyze"]]), default=None)
        if modified >= max(1000, live * STALE_ANALYZE_PERCENT / 100) and (last_analyze is None or (_age_days(last_analyze) or 0) > 1):
            out.append({"level": "info", "kind": "stale_statistics", "object": name, "title": f"{modified:,} rows changed since the statistics were last gathered",
                        "detail": "The planner chooses query plans from table statistics. Out-of-date statistics can make it choose slow plans.", "sql": f"ANALYZE {ref};",
                        "table": [t["schema"], t["table"]]})
        if live >= 10_000 and not t["last_vacuum"] and not t["last_autovacuum"] and t["kind"] == "r" and (t["updated"] or 0) + (t["deleted"] or 0) > 0:
            out.append({"level": "info", "kind": "never_vacuumed", "object": name, "title": "Never vacuumed",
                        "detail": "No manual or automatic vacuum is recorded for this table (since the statistics were last reset).", "sql": f"VACUUM (ANALYZE) {ref};",
                        "table": [t["schema"], t["table"]]})
        if live >= 10_000 and (t["seq_scan"] or 0) > 100 and (t["seq_tup_read"] or 0) > 1_000_000 and (t["seq_scan"] or 0) > 5 * (t["idx_scan"] or 0):
            out.append({"level": "info", "kind": "sequential_scans", "object": name,
                        "title": f"Read in full {t['seq_scan']:,} times ({t['seq_tup_read']:,} rows), against {t['idx_scan']:,} index lookups",
                        "detail": "Queries on this table mostly scan all of it. If they filter or join on a column, an index on that column may help. Check the slow statements on the Dashboards page.",
                        "sql": ""})
        if t["xid_age"] is not None and t["xid_age"] >= freeze_max * FREEZE_WARNING:
            share = t["xid_age"] / freeze_max
            out.append({"level": "critical" if share >= FREEZE_CRITICAL else "warning", "kind": "freeze_age", "object": name,
                        "title": f"Transaction ID age is {share * 100:.0f}% of the forced-vacuum limit",
                        "detail": "At 100% PostgreSQL starts an urgent anti-wraparound vacuum that cannot be postponed and can slow the server for a long time. Vacuum this table sooner.",
                        "sql": f"VACUUM (FREEZE, VERBOSE) {ref};", "table": [t["schema"], t["table"]]})
        reads, hits = t["heap_read"] or 0, t["heap_hit"] or 0
        if reads > 10_000 and hits * 100 / (reads + hits) < 90:
            out.append({"level": "info", "kind": "cache", "object": name, "title": f"Only {hits * 100 / (reads + hits):.0f}% of table reads come from memory",
                        "detail": "Most reads go to disk. The table may be too big for the memory available (shared_buffers / the operating system cache), or scanned too often.", "sql": ""})
        updated = t["updated"] or 0
        if updated >= 10_000 and (t["hot_updated"] or 0) * 100 / updated < 20 and (t["indexes"] or 0) > 0:
            out.append({"level": "info", "kind": "hot_updates", "object": name, "title": f"Only {(t['hot_updated'] or 0) * 100 / updated:.0f}% of updates are cheap (HOT)",
                        "detail": "Each other update also rewrites every index of the table. A lower fillfactor on the table, or fewer indexes on frequently updated columns, reduces that.",
                        "sql": f"ALTER TABLE {ref} SET (fillfactor = 90);"})
        if not t["has_primary_key"] and live > 0 and t["kind"] == "r":
            out.append({"level": "info", "kind": "no_primary_key", "object": name, "title": "No primary key",
                        "detail": "Without a primary key rows cannot be told apart reliably: the data grid cannot edit it, and logical replication cannot publish updates.", "sql": ""})
    by_signature = {}
    for ix in data["indexes"]:
        name, ref = _name(ix, "index"), f"{quote(ix['schema'])}.{quote(ix['index'])}"
        if not ix["valid"]:
            out.append({"level": "critical", "kind": "index_invalid", "object": name, "title": "Invalid index",
                        "detail": f"A CREATE INDEX CONCURRENTLY on {ix['schema']}.{ix['table']} failed and left this index unusable. It costs writes and gives nothing. Drop it and create it again.",
                        "sql": f"DROP INDEX CONCURRENTLY {ref};"})
            continue
        signature = (ix["table_oid"], ix["key"], ix["opclass"], ix["options"], ix["predicate"], ix["expressions"])
        by_signature.setdefault(signature, []).append(ix)
        stats_days = data["stats_age_days"]
        if (ix["scans"] == 0 and not ix["unique"] and not ix["primary"] and not ix["constraints"] and ix["size"] >= UNUSED_INDEX_MIN_BYTES
                and (stats_days is None or stats_days >= UNUSED_INDEX_MIN_DAYS)):
            out.append({"level": "warning", "kind": "index_unused", "object": name, "title": f"Never used ({_size(ix['size'])} of space)",
                        "detail": f"No query has used this index since the statistics were reset ({'about ' + str(stats_days) + ' days ago' if stats_days is not None else 'unknown'}). "
                                  "It still slows every write to the table. Check that no rare job (month end, reports) needs it before dropping it.",
                        "sql": f"DROP INDEX CONCURRENTLY {ref};"})
    for group in by_signature.values():
        if len(group) > 1:
            keep = sorted(group, key=lambda i: (not i["primary"], not i["unique"], -i["scans"], i["index"]))[0]
            for ix in group:
                if ix is keep or ix["primary"] or ix["unique"] or ix["constraints"]:
                    continue
                out.append({"level": "warning", "kind": "index_duplicate", "object": _name(ix, "index"), "title": f"Same as {keep['index']} ({_size(ix['size'])} of space)",
                            "detail": f"This index has exactly the same columns, order and conditions as {keep['schema']}.{keep['index']}. One of them is wasted space and slows writes.",
                            "sql": f"DROP INDEX CONCURRENTLY {quote(ix['schema'])}.{quote(ix['index'])};"})
    order = {"critical": 0, "warning": 1, "info": 2}
    out.sort(key=lambda f: (order[f["level"]], f["object"]))
    return out


def _size(value):
    return pg.size_text(value or 0)


def summary(data):
    tables, indexes, found = data["tables"], data["indexes"], data["findings"]
    count = lambda kind: sum(1 for f in found if f["kind"] == kind)
    unused = [ix for ix in indexes if any(f["kind"] == "index_unused" and f["object"] == _name(ix, "index") for f in found)]
    duplicate = [ix for ix in indexes if any(f["kind"] == "index_duplicate" and f["object"] == _name(ix, "index") for f in found)]
    return {"tables": data["table_count"], "indexes": data["index_count"], "table_bytes": sum(t["total"] or 0 for t in tables),
            "dead_rows": sum(t["dead"] or 0 for t in tables), "needing_vacuum": count("dead_rows"), "stale_statistics": count("stale_statistics"),
            "near_freeze": count("freeze_age"), "unused_indexes": len(unused), "unused_bytes": sum(ix["size"] for ix in unused),
            "duplicate_indexes": len(duplicate), "duplicate_bytes": sum(ix["size"] for ix in duplicate), "invalid_indexes": count("index_invalid"),
            "critical": sum(1 for f in found if f["level"] == "critical"), "warnings": sum(1 for f in found if f["level"] == "warning"),
            "information": sum(1 for f in found if f["level"] == "info")}


def bloat(server, database, schema, table):
    """Exact dead-space measure of one table (and its indexes) with pgstattuple, if it is installed. It reads the whole table, so a big one is refused."""
    with pg.connect(server, database) as conn:
        conn.execute("SELECT set_config('statement_timeout', %s, false)", (str(int(settings.WATCH_SAMPLE_TIMEOUT_MS) * 8), ))
        if not conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'pgstattuple'").fetchone():
            raise pg.PgError("The pgstattuple extension is not installed in this database (CREATE EXTENSION pgstattuple), so there is no exact measure. "
                             "The dead-row figures on this page are estimates from PostgreSQL's statistics.")
        ref = f"{quote(schema)}.{quote(table)}"
        size = conn.execute("SELECT pg_relation_size(%s::regclass)", (ref,)).fetchone()[0]
        if size > BLOAT_SCAN_MAX_BYTES:
            raise pg.PgError(f"This table is {pg.size_text(size)}: reading all of it to measure it is too heavy. Measure a smaller table, or run pgstattuple yourself in a quiet hour.")
        row = conn.execute("SELECT table_len, tuple_count, dead_tuple_count, dead_tuple_percent, free_space, free_percent FROM pgstattuple(%s::regclass)", (ref,)).fetchone()
        conn.rollback()
    return {"table_len": row[0], "tuples": row[1], "dead_tuples": row[2], "dead_percent": _clean(row[3]), "free_space": row[4], "free_percent": _clean(row[5]),
            "reclaimable": (row[4] or 0) + int((row[0] or 0) * float(row[3] or 0) / 100)}