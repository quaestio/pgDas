"""The settings that explain the charts: what each one does, a link to its page in the PostgreSQL manual for the server's own version, and
(for Super Users) changing it with ALTER SYSTEM.

Changing a setting writes it to postgresql.auto.conf on the monitored server and asks PostgreSQL to reload its configuration. It is
reviewed first (what the value is now, what it would become, whether a restart is needed), needs the word ALTER, is recorded in the audit log
before it is made, and is refused unless the login PGDAS uses may do it (a PostgreSQL superuser, or from PostgreSQL 15 a login granted
ALTER SYSTEM on that parameter). Only the settings below can be changed, and `shared_preload_libraries` is shown but not editable: a wrong
value there stops PostgreSQL from starting at its next restart.
"""
import re
import time

from psycopg import sql

from audit.models import record
from explorer import changes, pg
from explorer.pg import PgError

# name: (manual page, what it does). The page is the one of the PostgreSQL manual that documents it (autovacuum moved in 18: see docs_url).
CATALOG = {
    "max_connections": ("runtime-config-connection", "The most sessions that may be connected at once. Connection charts are read against this limit."),
    "shared_buffers": ("runtime-config-resource", "PostgreSQL's own cache of table and index pages. Too small means more disk reads (a low cache hit). About 25% of RAM is a common start."),
    "work_mem": ("runtime-config-resource", "Memory for one sort or hash step of a query. Sorts that do not fit spill to temporary files (see the temp charts)."),
    "maintenance_work_mem": ("runtime-config-resource", "Memory for VACUUM, CREATE INDEX and similar jobs. More makes them faster."),
    "effective_cache_size": ("runtime-config-query", "The planner's guess of all cache available (PostgreSQL plus the operating system). It reserves nothing; it steers the plans."),
    "max_wal_size": ("runtime-config-wal", "How much WAL may build up before a checkpoint is forced. A write-heavy server that reaches it often shows many 'requested' checkpoints."),
    "min_wal_size": ("runtime-config-wal", "WAL kept for reuse instead of being deleted. Affects disk use and how quickly WAL can be recycled."),
    "checkpoint_timeout": ("runtime-config-wal", "The longest time between checkpoints. Write spikes on the charts follow this rhythm."),
    "autovacuum": ("runtime-config-autovacuum", "Background cleanup of dead rows and refreshing of statistics. Leave it on: the dead-row and bloat charts depend on it."),
    "autovacuum_freeze_max_age": ("runtime-config-autovacuum", "The transaction age at which a forced anti-wraparound vacuum starts. The XID-age chart is read against it."),
    "track_io_timing": ("runtime-config-statistics", "Times disk reads and writes. The I/O time charts stay empty while it is off; turning it on costs a little overhead."),
    "track_counts": ("runtime-config-statistics", "Collects table and index activity statistics. Autovacuum and most activity charts need it."),
    "wal_level": ("runtime-config-wal", "How much detail goes into the write-ahead log: minimal, replica (backups and streaming copies) or logical (logical replication)."),
    "synchronous_commit": ("runtime-config-wal", "Whether a commit waits for its WAL to reach disk (on) or not (off: faster, but the last moments of commits can be lost in a crash)."),
    "random_page_cost": ("runtime-config-query", "The planner's cost of a random disk read. 4 suits spinning disks; on SSD 1.1 to 2 is usual, because a high value discourages index use."),
    "max_worker_processes": ("runtime-config-resource", "Background worker slots in total. It is the ceiling for the parallel workers below."),
    "max_parallel_workers": ("runtime-config-resource", "Workers that parallel queries may use at the same time."),
    "log_min_duration_statement": ("runtime-config-logging", "Writes to the server log every statement slower than this many milliseconds. -1 turns it off; 0 logs everything."),
    "shared_preload_libraries": ("runtime-config-client", "Extensions loaded when the server starts. pg_stat_statements (the statement charts) needs to be listed here."),
}
READ_ONLY = {"shared_preload_libraries": "A wrong value here stops PostgreSQL from starting after its next restart, so it is not changed from this page. Edit postgresql.conf, or run ALTER SYSTEM in psql."}
CONFIRM_WORD = changes.CONFIRM_WORDS["alter"]

_MEMORY = {"B": 1, "kB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
_TIME = {"us": 0.001, "ms": 1, "s": 1000, "min": 60000, "h": 3600000, "d": 86400000}
_SETTING_UNIT = {"B": 1, "kB": 1024, "8kB": 8192, "ms": 1, "s": 1000, "min": 60000, "us": 0.001}
_BOOLEANS = {"on", "off", "true", "false", "yes", "no", "1", "0"}
_NUMBER = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*$")


def docs_url(name, version_num):
    """The page of the manual for this setting, for the server's major version (autovacuum was moved to the 'vacuum' page in 18)."""
    page = CATALOG[name][0]
    major = version_num // 10000 if version_num else 18
    if page == "runtime-config-autovacuum" and major >= 18:
        page = "runtime-config-vacuum"
    return f"https://www.postgresql.org/docs/{major}/{page}.html#GUC-{name.upper().replace('_', '-')}"


def describe(version_num):
    """What the Dashboards page shows next to each setting: {name: {text, url, editable, reason}}."""
    return {name: {"text": text, "url": docs_url(name, version_num), "editable": name not in READ_ONLY, "reason": READ_ONLY.get(name, "")}
            for name, (_, text) in CATALOG.items()}


def _read(conn, name):
    row = conn.execute("""SELECT setting, unit, context, vartype, min_val, max_val, enumvals, boot_val, pending_restart, current_setting(name)
                          FROM pg_settings WHERE name = %s""", (name,)).fetchone()
    if row is None:
        raise PgError(f"This server has no setting called {name}.")
    keys = ("setting", "unit", "context", "vartype", "min", "max", "enum", "boot", "pending_restart", "shown")
    return dict(zip(keys, row))


def _check_rights(conn, name):
    login, superuser = conn.execute("SELECT rolname, rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()
    if superuser:
        return
    if conn.info.server_version >= 150000 and conn.execute("SELECT has_parameter_privilege(current_user, %s, 'ALTER SYSTEM')", (name,)).fetchone()[0]:
        return
    raise PgError(f'The login "{login}" that PGDAS uses for this server may not change its settings. ALTER SYSTEM needs a PostgreSQL superuser '
                  f'(or, from PostgreSQL 15, GRANT ALTER SYSTEM ON PARAMETER {name} TO {login}).')


def _range_text(info):
    unit = f" {info['unit']}" if info["unit"] else ""
    return f"{info['min']}{unit} to {info['max']}{unit}"


def _validate(name, text, info):
    """Say so up front when the value cannot be right. PostgreSQL checks it again when the setting is saved."""
    kind = info["vartype"]
    if kind == "bool":
        if text.lower() not in _BOOLEANS:
            raise PgError(f"{name} takes on or off.")
    elif kind == "enum":
        if text.lower() not in [v.strip('"') for v in (info["enum"] or [])]:
            raise PgError(f"{name} takes one of: {', '.join(v.strip(chr(34)) for v in info['enum'] or [])}.")
    elif kind in ("integer", "real"):
        match = _NUMBER.match(text)
        if not match:
            raise PgError(f"{name} takes a number" + (f" (with a unit such as {'MB or GB' if info['unit'] in ('B', 'kB', '8kB') else 'ms, s or min'} if you like)." if info["unit"] else "."))
        number, unit = float(match[1]), match[2]
        if unit:
            table = _MEMORY if info["unit"] in ("B", "kB", "8kB") else _TIME if info["unit"] in ("us", "ms", "s", "min") else {}
            if unit not in table:
                raise PgError(f"“{unit}” is not a unit {name} understands" + (f" (use {', '.join(table)})." if table else ": give a plain number."))
            number = number * table[unit] / _SETTING_UNIT[info["unit"]]
        if kind == "integer" and abs(number - round(number)) > 1e-9:
            raise PgError(f"{name} must be a whole number" + (f" of {info['unit']}." if info["unit"] else "."))
        if float(info["min"]) > number or number > float(info["max"]):
            raise PgError(f"{text} is outside what {name} accepts on this server: {_range_text(info)}.")


def _statement(conn, name, value):
    if value is None:
        return sql.SQL("ALTER SYSTEM RESET {}").format(sql.Identifier(name)).as_string(conn)
    return sql.SQL("ALTER SYSTEM SET {} = {}").format(sql.Identifier(name), sql.Literal(value)).as_string(conn)


def _request(name, value, reset):
    if name not in CATALOG:
        raise PgError("That setting is not one this page changes.")
    if name in READ_ONLY:
        raise PgError(READ_ONLY[name])
    if reset:
        return None
    text = str(value if value is not None else "").strip()
    if not text:
        raise PgError("Give the new value, or choose to go back to the default.")
    if len(text) > 100 or any(ord(c) < 32 for c in text):
        raise PgError("That is not a valid value.")
    return text


def preview(server, name, value, reset):
    text = _request(name, value, reset)
    with pg.connect(server) as conn:
        _check_rights(conn, name)
        info = _read(conn, name)
        if text is not None:
            _validate(name, text, info)
        statement = _statement(conn, name, text)
    restart = info["context"] == "postmaster"
    target = text if text is not None else f"the default ({info['boot']}{' ' + info['unit'] if info['unit'] and info['boot'] not in (None, '') else ''})"
    notes = [{"text": ("Needs a restart of PostgreSQL to take effect. PGDAS cannot restart it: until someone does, the server keeps running with "
                       f"{info['shown']}.") if restart else "PGDAS asks PostgreSQL to reload its configuration right after saving, so this applies at once "
                       "(sessions that are already open may keep their own value until they reconnect).", "bad": restart},
             {"text": "The value is written to postgresql.auto.conf on the server. Going back to the default removes that line.", "bad": False}]
    if info["pending_restart"]:
        notes.append({"text": "A change to this setting is already waiting for a restart.", "bad": True})
    return {"kind": "alter", "statement": statement, "count": 1, "objects": [{"name": name, "text": f"~ change setting {name} — {info['shown']} → {target}", "rows": None, "cascade": False}],
            "notes": notes, "title": "CHANGE SERVER SETTING?", "headline": name, "headline_label": f"will change from {info['shown']} to {target}.",
            "warning": "This changes the configuration of the whole PostgreSQL server, for every database and every user. It cannot be rolled back; "
                       "it can be changed back with another change here.", "confirm_word": CONFIRM_WORD, "confirm_prompt": None, "button": "Change setting",
            "side_effects": [], "columns": [], "sample": [], "has_where": True, "risky": True,
            "fingerprint": {"name": name, "value": text, "current": info["shown"]}}


def execute(server, name, value, reset, fingerprint, confirm, user):
    text = _request(name, value, reset)
    fingerprint = fingerprint if isinstance(fingerprint, dict) else {}
    with pg.connect_maintenance(server, writable=True) as conn:           # ALTER SYSTEM cannot run inside a transaction
        _check_rights(conn, name)
        info = _read(conn, name)
        if text is not None:
            _validate(name, text, info)
        if fingerprint != {"name": name, "value": text, "current": info["shown"]}:
            raise changes._changed_since("the setting is not what you were shown.")
        if confirm != CONFIRM_WORD:
            raise PgError(f"Type {CONFIRM_WORD} in capital letters to confirm.")
        statement = _statement(conn, name, text)
        payload = {"kind": "alter", "database": None, "object_type": "SERVER SETTING", "statement": statement, "risky": True, "before": [],
                   "changes": [{"action": "alter", "kind": "setting", "name": name, "detail": f"{info['shown']} → {text if text is not None else 'default'}"}]}
        try:                                                              # not undoable by the engine, so the record comes first and is corrected if it fails
            entry = record(user, "alter", server, statement, ok=True, rows=1, payload=payload)
        except Exception:
            raise PgError("PGDAS could not save the audit record, so nothing was changed.")
        try:
            changes._run(conn, statement)
            conn.execute("SELECT pg_reload_conf()")
        except PgError as error:
            entry.ok = False
            entry.detail = f"{entry.detail}\n(failed: {str(error)[:200]})"
            entry.save(update_fields=["ok", "detail"])
            raise
        after = info
        for _ in range(10):                                               # the reload reaches this connection a moment later
            after = _read(conn, name)
            if after["shown"] != info["shown"] or after["pending_restart"]:
                break
            time.sleep(0.2)
    if after["pending_restart"]:
        outcome = f"saved; it takes effect after PostgreSQL is restarted (it still runs with {after['shown']})"
    elif after["shown"] != info["shown"]:
        outcome = f"now {after['shown']}"
    else:
        outcome = "saved and a reload was requested"
    return {"count": 1, "entry_id": entry.pk, "restart": bool(after["pending_restart"]), "value": after["shown"],
            "message": f"{name}: {outcome}. Recorded in the audit log as entry #{entry.pk}."}
