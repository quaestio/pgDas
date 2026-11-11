"""Run pg_dump and pg_restore for saved servers. The password goes to the tools through the environment, never on the
command line or into the audit log."""
import os
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from psycopg import sql
from django.utils import timezone

from audit.models import record
from explorer import pg
from explorer.pg import PgError

from .models import Backup

DUMP_TIMEOUT = 6 * 3600
RESTORE_WORD = "RESTORE"
PATTERN = re.compile(r'[A-Za-z0-9_$.*?"\-]{1,200}')
MAX_PATTERNS = 50


def tool(name):
    """Full path of a PostgreSQL client tool: PGDAS_PG_BIN first, then PATH."""
    exe = name + (".exe" if os.name == "nt" else "")
    if settings.PG_BIN:
        candidate = Path(settings.PG_BIN) / exe
        if candidate.is_file():
            return str(candidate)
        raise PgError(f"{exe} was not found in PGDAS_PG_BIN ({settings.PG_BIN}).")
    found = shutil.which(name)
    if not found:
        raise PgError(f"{exe} was not found. Install the PostgreSQL client tools or set PGDAS_PG_BIN to their bin folder.")
    return found


def _environment(server):
    from servers import netpolicy
    env = dict(os.environ, PGPASSWORD=server.get_password(), PGSSLMODE=server.sslmode, PGCONNECT_TIMEOUT="15")
    try:
        pinned = netpolicy.check(server.host)           # the same rule as every other connection; pg_dump connects to the checked address
    except netpolicy.AddressNotAllowed as error:
        raise PgError(str(error))
    if pinned:
        env["PGHOSTADDR"] = pinned
    return env


def _connection_args(server):
    return ["-h", server.host, "-p", str(server.port), "-U", server.username]


def _patterns(values, label):
    """Names or patterns (one per line or comma separated): letters, digits and _ $ . * ? " - only, so nothing can pass for an option."""
    found = []
    for value in values:
        for part in re.split(r"[\r\n,]+", value or ""):
            part = part.strip()
            if not part:
                continue
            if not PATTERN.fullmatch(part):
                raise PgError(f'"{part[:40]}" is not a valid {label}. Use letters, digits and _ . * ? only, for example public.orders or public.log_*.')
            if part not in found:
                found.append(part)
    if len(found) > MAX_PATTERNS:
        raise PgError(f"Give at most {MAX_PATTERNS} {label}s.")
    return found


def parse_options(schemas=(), tables="", skip_data="", compression=""):
    """The options of a backup from what the form sent. Empty means everything, default compression. Raises PgError when invalid."""
    options = {}
    for key, values, label in (("schemas", list(schemas), "schema name"), ("tables", [tables], "table name"), ("skip_data", [skip_data], "table name")):
        found = _patterns(values, label)
        if found:
            options[key] = found
    if str(compression).strip() != "":
        try:
            level = int(str(compression).strip())
        except ValueError:
            level = -1
        if not 0 <= level <= 9:
            raise PgError("Compression must be a number from 0 (none) to 9 (smallest).")
        options["compression"] = level
    return options


def describe(options):
    """The options in one line for the Backups page; empty for a plain full backup."""
    parts = []
    if options.get("schemas"):
        parts.append("only schemas: " + ", ".join(options["schemas"]))
    if options.get("tables"):
        parts.append("only tables: " + ", ".join(options["tables"]))
    if options.get("skip_data"):
        parts.append("without the data of: " + ", ".join(options["skip_data"]))
    if options.get("compression") is not None:
        parts.append(f"compression {options['compression']}")
    return " · ".join(parts)


def _option_args(options):
    """Long options with `=` so a value can never be read as another option."""
    args = [f"--schema={s}" for s in options.get("schemas", [])]
    args += [f"--table={t}" for t in options.get("tables", [])]
    args += [f"--exclude-table-data={d}" for d in options.get("skip_data", [])]
    if options.get("compression") is not None:
        args.append(f"--compress={options['compression']}")
    return args


def _run(args, server, tolerate=False, output=False):
    try:
        done = subprocess.run(args, env=_environment(server), capture_output=True, text=True, timeout=DUMP_TIMEOUT,
                              stdin=subprocess.DEVNULL, errors="replace")
    except subprocess.TimeoutExpired:
        raise PgError("The operation took too long and was stopped.")
    except OSError as error:
        raise PgError(str(error))
    if done.returncode == 1 and tolerate and "errors ignored on restore" in (done.stderr or ""):
        return done.stderr                                          # a partial backup: some objects needed what was left out
    if done.returncode:
        raise PgError((done.stderr or done.stdout or f"exit code {done.returncode}").strip()[-2000:])
    return done.stdout if output else done.stderr


def _file_name(server, database, now, pk):
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{server.name}_{database}").strip("-")
    return f"{slug}_{now:%Y%m%d-%H%M%S}_{pk}.dump"             # the id keeps two backups in the same second apart


def make_backup(server, database, user=None, kind="manual", options=None):
    """Dump one database (or the chosen schemas and tables of it) to a file in the backup folder. Returns the Backup row."""
    now = timezone.now()
    options = options or {}
    backup = Backup.objects.create(server=server, server_name=server.name, database=database, kind=kind, created_by=user, options=options)
    backup.filename = _file_name(server, database, now, backup.pk)
    backup.save(update_fields=["filename"])
    path = backup.path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _run([tool("pg_dump"), *_connection_args(server), "-d", database, "-Fc", "--no-password", *_option_args(options), "-f", str(path)], server)
        backup.status, backup.size = "ok", path.stat().st_size
    except PgError as error:
        backup.status, backup.error = "failed", str(error)
        path.unlink(missing_ok=True)
        backup.filename = ""
    backup.finished_at = timezone.now()
    backup.save()
    record(user, "backup.create" if kind == "manual" else "backup.scheduled", server,
           f"Backup of {database}" + (f" ({describe(options)})" if options else "") + (f" -> {backup.filename} ({backup.size} bytes)" if backup.status == "ok" else f" failed: {backup.error}"),
           ok=backup.status == "ok")
    return backup


_TABLE_ENTRY = re.compile(r'\bTABLE(?: DATA)? ("(?:[^"]|"")+"|[A-Za-z_][\w$]*) ')


def _schemas_of_tables(backup):
    """`pg_dump --table` saves the tables but not their schemas. The schemas named in the dump's table of contents, so the restore
    can create them first (public already exists in a new database)."""
    listing = _run([tool("pg_restore"), "--list", str(backup.path)], backup.server, output=True)
    found = {m.group(1) for line in listing.splitlines() for m in [_TABLE_ENTRY.search(line)] if m}
    return sorted(s for s in found if s != "public" and not s.startswith("pg_"))


def restore_backup(backup, new_name, user):
    """Restore into a NEW database, so nothing existing is overwritten."""
    if not backup.available:
        raise PgError("The backup file is not available.")
    server = backup.server
    if server is None:
        raise PgError("The server of this backup is no longer saved.")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,62}", new_name or ""):
        raise PgError("Give the new database a plain name: letters, digits and underscores, not starting with a digit.")
    with pg.connect_maintenance(server, avoid=new_name, writable=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (new_name,)).fetchone():
            raise PgError(f'The database "{new_name}" already exists. Restore goes into a new database; pick another name.')
        conn.execute(f'CREATE DATABASE "{new_name}"')
    try:
        if backup.options.get("tables") and not backup.options.get("schemas"):
            with pg.connect_rw(server, new_name) as conn:
                for schema in _schemas_of_tables(backup):
                    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema.strip('"').replace('""', '"'))))
                conn.commit()
        # A full backup must restore without a single error. One that left things out may name what is missing (a foreign key to a
        # table whose data was skipped, a trigger's function in a schema that was not saved), so there errors are only reported.
        warnings = _run([tool("pg_restore"), *_connection_args(server), "-d", new_name, "--no-password", "--no-owner", "--no-privileges",
                         *([] if backup.partial else ["--exit-on-error"]), str(backup.path)], server, tolerate=backup.partial)
    except PgError as error:
        with pg.connect_maintenance(server, avoid=new_name, writable=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{new_name}" WITH (FORCE)')
        record(user, "backup.restore", server, f"Restore of backup #{backup.pk} into {new_name} failed: {error}", ok=False)
        raise
    record(user, "backup.restore", server, f"Restored backup #{backup.pk} ({backup.database}) into new database {new_name}")
    return (warnings or "").strip()


def delete_backup(backup, user):
    if backup.path:
        backup.path.unlink(missing_ok=True)
    record(user, "backup.delete", backup.server, f"Deleted backup #{backup.pk} of {backup.database}")
    backup.delete()


def apply_retention(schedule):
    """Keep only the newest `keep` good scheduled backups of the schedule's database; older files are deleted."""
    good = Backup.objects.filter(server=schedule.server, database=schedule.database, kind="scheduled", status="ok")
    for old in list(good)[schedule.keep:]:
        if old.path:
            old.path.unlink(missing_ok=True)
        old.delete()


def due(schedule, now=None):
    now = now or timezone.now()
    return schedule.enabled and (schedule.last_run is None or schedule.last_run + timedelta(hours=schedule.every_hours) <= now)


def run_due(now=None):
    """Run every schedule that is due. Returns the Backup rows made."""
    from .models import BackupSchedule
    made = []
    for schedule in BackupSchedule.objects.select_related("server"):
        if not due(schedule, now):
            continue
        backup = make_backup(schedule.server, schedule.database, None, "scheduled", schedule.options)
        schedule.last_run = timezone.now()
        schedule.save(update_fields=["last_run"])
        if backup.status == "ok":
            apply_retention(schedule)
        made.append(backup)
    return made
