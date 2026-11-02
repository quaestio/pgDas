"""Run pg_dump and pg_restore for saved servers. The password goes to the tools through the environment, never on the
command line or into the audit log."""
import os
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from audit.models import record
from explorer import pg
from explorer.pg import PgError

from .models import Backup

DUMP_TIMEOUT = 6 * 3600
RESTORE_WORD = "RESTORE"


def tool(name):
    """Full path of a PostgreSQL client tool: PGDECK_PG_BIN first, then PATH."""
    exe = name + (".exe" if os.name == "nt" else "")
    if settings.PG_BIN:
        candidate = Path(settings.PG_BIN) / exe
        if candidate.is_file():
            return str(candidate)
        raise PgError(f"{exe} was not found in PGDECK_PG_BIN ({settings.PG_BIN}).")
    found = shutil.which(name)
    if not found:
        raise PgError(f"{exe} was not found. Install the PostgreSQL client tools or set PGDECK_PG_BIN to their bin folder.")
    return found


def _environment(server):
    env = dict(os.environ, PGPASSWORD=server.get_password(), PGSSLMODE=server.sslmode, PGCONNECT_TIMEOUT="15")
    return env


def _connection_args(server):
    return ["-h", server.host, "-p", str(server.port), "-U", server.username]


def _run(args, server):
    try:
        done = subprocess.run(args, env=_environment(server), capture_output=True, text=True, timeout=DUMP_TIMEOUT,
                              stdin=subprocess.DEVNULL, errors="replace")
    except subprocess.TimeoutExpired:
        raise PgError("The operation took too long and was stopped.")
    except OSError as error:
        raise PgError(str(error))
    if done.returncode:
        raise PgError((done.stderr or done.stdout or f"exit code {done.returncode}").strip()[-2000:])
    return done.stderr


def _file_name(server, database, now, pk):
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{server.name}_{database}").strip("-")
    return f"{slug}_{now:%Y%m%d-%H%M%S}_{pk}.dump"             # the id keeps two backups in the same second apart


def make_backup(server, database, user=None, kind="manual"):
    """Dump one database to a file in the backup folder. Returns the Backup row (status ok or failed)."""
    now = timezone.now()
    backup = Backup.objects.create(server=server, server_name=server.name, database=database, kind=kind, created_by=user)
    backup.filename = _file_name(server, database, now, backup.pk)
    backup.save(update_fields=["filename"])
    path = backup.path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _run([tool("pg_dump"), *_connection_args(server), "-d", database, "-Fc", "--no-password", "-f", str(path)], server)
        backup.status, backup.size = "ok", path.stat().st_size
    except PgError as error:
        backup.status, backup.error = "failed", str(error)
        path.unlink(missing_ok=True)
        backup.filename = ""
    backup.finished_at = timezone.now()
    backup.save()
    record(user, "backup.create" if kind == "manual" else "backup.scheduled", server,
           f"Backup of {database}" + (f" -> {backup.filename} ({backup.size} bytes)" if backup.status == "ok" else f" failed: {backup.error}"),
           ok=backup.status == "ok")
    return backup


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
        warnings = _run([tool("pg_restore"), *_connection_args(server), "-d", new_name, "--no-password",
                         "--no-owner", "--no-privileges", "--exit-on-error", str(backup.path)], server)
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
        backup = make_backup(schedule.server, schedule.database, None, "scheduled")
        schedule.last_run = timezone.now()
        schedule.save(update_fields=["last_run"])
        if backup.status == "ok":
            apply_retention(schedule)
        made.append(backup)
    return made
