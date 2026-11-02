"""Explicit, allow-listed monitoring settings; never restart a managed server."""
import csv

from psycopg import sql

from audit.models import record
from explorer import pg


FLAGS = {
    "track_activities": ("Activity tracking", "Shows running queries. Turning it off reduces session visibility."),
    "track_counts": ("Table statistics", "Used by monitoring and autovacuum. Keep enabled for normal operation."),
    "track_io_timing": ("I/O timing", "Measures disk wait times; may add timing overhead."),
    "log_lock_waits": ("Log lock waits", "Writes long lock waits to the PostgreSQL server log."),
    "pg_stat_statements.track": ("Statement collection", "Collects statement costs across this PostgreSQL server."),
}


def status(server):
    with pg.connect(server) as conn:
        database = conn.execute("SELECT current_database()").fetchone()[0]
        rows = conn.execute("SELECT name, setting, pending_restart FROM pg_settings WHERE name = ANY(%s)",
                            (list(FLAGS) + ["shared_preload_libraries", "compute_query_id"],)).fetchall()
        settings = {r[0]: r for r in rows}
        extension = bool(conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'").fetchone())
        available = bool(conn.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'pg_stat_statements'").fetchone())
    return {"database": database, "extension": extension, "available": available,
            "preload": settings.get("shared_preload_libraries", (None, "Unavailable", False))[1],
            "restart": settings.get("shared_preload_libraries", (None, None, False))[2],
            "query_id": settings.get("compute_query_id", (None, "Unavailable", False))[1],
            "flags": [{"name": name, "label": label, "description": description,
                       "value": settings[name][1] if name in settings else "Unavailable",
                       "available": name in settings} for name, (label, description) in FLAGS.items()]}


def change(server, name, enabled, user):
    if name not in FLAGS and name not in ("extension", "preload", "compute_query_id"):
        raise pg.PgError("Unknown monitoring setting.")
    detail = f"{name}: {'on' if enabled else 'off'}"
    if name == "extension":
        with pg.connect_rw(server) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements" if enabled else
                         "DROP EXTENSION IF EXISTS pg_stat_statements")
            record(user, "monitor.setting", server, detail)
            conn.commit()
        return "Database extension updated. Statement history also requires the preload library and query IDs."
    with pg.connect_maintenance(server, writable=True) as conn:
        value = ("top" if enabled else "none") if name == "pg_stat_statements.track" else ("on" if enabled else "off")
        if name == "preload":
            if not enabled:
                raise pg.PgError("Use Statement collection Off to stop collection without unloading the library.")
            # Use the last configured value, including changes waiting for restart, so other libraries survive.
            if not conn.execute("SELECT 1 FROM pg_available_extensions WHERE name='pg_stat_statements'").fetchone():
                raise pg.PgError("Install the PostgreSQL contrib package on the database server first.")
            configured = conn.execute("SELECT setting FROM pg_file_settings WHERE name='shared_preload_libraries' "
                                      "ORDER BY seqno DESC LIMIT 1").fetchone()
            value = configured[0] if configured else conn.execute("SHOW shared_preload_libraries").fetchone()[0]
            # PostgreSQL parses the existing list; append without rewriting or removing any entries.
            libraries = next(csv.reader([value], skipinitialspace=True))
            if any(item.strip() in ("pg_stat_statements", "$libdir/pg_stat_statements") for item in libraries):
                return "Statement history is already configured for preload. If it is not active, restart PostgreSQL on its host."
            value = value + (",pg_stat_statements" if value.strip() else "pg_stat_statements")
            name = "shared_preload_libraries"
        record(user, "monitor.setting.request", server, detail)
        conn.execute(sql.SQL("ALTER SYSTEM SET {} = {}").format(sql.Identifier(name), sql.Literal(value)))
        reloaded = conn.execute("SELECT pg_reload_conf()").fetchone()[0]
        record(user, "monitor.setting", server, detail + ("; reload requested" if reloaded else "; reload refused"))
    if name == "shared_preload_libraries":
        return "Preload configuration saved. Restart PostgreSQL on its host, then enable the database extension. PGDAS does not restart servers."
    return "Setting saved. " + ("Reload requested; refresh to verify the effective value. Existing session overrides may still apply." if reloaded else "PostgreSQL refused the reload; reload its configuration on the host.")
