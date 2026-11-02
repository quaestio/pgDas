from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from audit.models import record
from explorer import pg
from explorer.pg import PgError
from servers.models import ServerConnection

from . import dump
from .models import Backup, BackupSchedule

staff_required = user_passes_test(lambda user: user.is_staff)


def _back():
    return redirect("backup_list")


@login_required
@staff_required
def backup_list(request):
    return render(request, "backups/list.html", {
        "backups": Backup.objects.select_related("created_by")[:200],
        "schedules": BackupSchedule.objects.select_related("server"),
        "servers": ServerConnection.objects.all(),
        "restore_word": dump.RESTORE_WORD,
    })


@login_required
@staff_required
def database_choices(request):
    """The databases of one saved server, for the dropdowns on the Backups page."""
    server = get_object_or_404(ServerConnection, pk=request.GET.get("server") or 0)
    try:
        with pg.connect(server) as conn:
            names = [d["name"] for d in pg.list_databases(conn) if d["name"] != "template0"]
    except PgError as error:
        return JsonResponse({"error": str(error)}, status=502)
    return JsonResponse({"databases": names})


@login_required
@staff_required
@require_POST
def backup_create(request):
    server = get_object_or_404(ServerConnection, pk=request.POST.get("server") or 0)
    database = request.POST.get("database", "").strip()
    if not database:
        messages.error(request, "Give the database to back up.")
        return _back()
    backup = dump.make_backup(server, database, request.user)
    if backup.status == "ok":
        messages.success(request, f"Backed up {database} ({backup.size:,} bytes).")
    else:
        messages.error(request, f"Backup of {database} failed: {backup.error}")
    return _back()


@login_required
@staff_required
def backup_download(request, pk):
    backup = get_object_or_404(Backup, pk=pk)
    if not backup.available:
        raise Http404("The backup file is not available.")
    record(request.user, "backup.download", backup.server, f"Downloaded backup #{backup.pk} of {backup.database}")
    return FileResponse(open(backup.path, "rb"), as_attachment=True, filename=backup.filename)


@login_required
@staff_required
@require_POST
def backup_delete(request, pk):
    backup = get_object_or_404(Backup, pk=pk)
    dump.delete_backup(backup, request.user)
    messages.success(request, "Backup deleted.")
    return _back()


@login_required
@staff_required
@require_POST
def backup_restore(request, pk):
    backup = get_object_or_404(Backup, pk=pk)
    if request.POST.get("confirm", "") != dump.RESTORE_WORD:
        messages.error(request, f"Type {dump.RESTORE_WORD} exactly to restore.")
        return _back()
    name = request.POST.get("new_name", "").strip()
    try:
        warnings = dump.restore_backup(backup, name, request.user)
    except PgError as error:
        messages.error(request, f"Could not restore: {error}")
    else:
        messages.success(request, f"Restored into the new database {name}." + (f" PostgreSQL said: {warnings}" if warnings else ""))
    return _back()


@login_required
@staff_required
@require_POST
def schedule_save(request):
    server = get_object_or_404(ServerConnection, pk=request.POST.get("server") or 0)
    database = request.POST.get("database", "").strip()
    try:
        every, keep = int(request.POST.get("every_hours", "")), int(request.POST.get("keep", ""))
    except ValueError:
        every = keep = 0
    if not database or every < 1 or keep < 1:
        messages.error(request, "Give a database, a number of hours of at least 1, and how many backups to keep (at least 1).")
        return _back()
    BackupSchedule.objects.update_or_create(server=server, database=database,
                                            defaults={"every_hours": every, "keep": keep, "enabled": True})
    record(request.user, "backup.schedule", server, f"Scheduled backup of {database} every {every} h, keeping {keep}")
    messages.success(request, f"{database} will be backed up every {every} hour{'s' if every != 1 else ''}, keeping the newest {keep}. "
                              "Run `manage.py run_backups` regularly (or with --loop) to make it happen.")
    return _back()


@login_required
@staff_required
@require_POST
def schedule_delete(request, pk):
    schedule = get_object_or_404(BackupSchedule, pk=pk)
    record(request.user, "backup.schedule", schedule.server, f"Removed the schedule for {schedule.database}")
    schedule.delete()
    messages.success(request, "Schedule removed. Existing backups are kept.")
    return _back()
