from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from audit.models import record
from explorer import pg
from explorer.pg import PgError
from servers.models import ServerConnection

from . import queries, features

staff_required = user_passes_test(lambda user: user.is_staff)


@login_required
@staff_required
def monitor_features(request):
    server = get_object_or_404(ServerConnection, pk=request.GET.get("server") or 0)
    try:
        return JsonResponse({"ok": True, **features.status(server)})
    except PgError as error:
        return JsonResponse({"ok": False, "error": str(error)}, status=400)


@login_required
@staff_required
@require_POST
def monitor_feature_change(request):
    server = get_object_or_404(ServerConnection, pk=request.POST.get("server") or 0)
    if request.POST.get("confirm") != "APPLY" or request.POST.get("enabled") not in ("on", "off"):
        return JsonResponse({"ok": False, "error": "Confirm the setting change with APPLY."}, status=400)
    try:
        message = features.change(server, request.POST.get("name", ""), request.POST["enabled"] == "on", request.user)
        return JsonResponse({"ok": True, "message": message})
    except PgError as error:
        record(request.user, "monitor.setting.failed", server, str(error), ok=False)
        return JsonResponse({"ok": False, "error": str(error)}, status=400)


@login_required
@staff_required
def monitor_page(request):
    return render(request, "monitor/page.html", {"servers": ServerConnection.objects.all()})


@login_required
@staff_required
def monitor_data(request):
    server = get_object_or_404(ServerConnection, pk=request.GET.get("server") or 0)
    try:
        slow_after = max(1, int(request.GET.get("slow_after") or 5))
    except ValueError:
        slow_after = 5
    try:
        return JsonResponse({"ok": True, **queries.snapshot(server, slow_after)})
    except PgError as error:
        return JsonResponse({"ok": False, "error": str(error)}, status=502)


@login_required
@staff_required
@require_POST
def monitor_stop(request):
    """Cancel the running query of a session (pg_cancel_backend) or end the session (pg_terminate_backend).
    Whether the login may do that is PostgreSQL's decision; its reason is shown when it refuses."""
    server = get_object_or_404(ServerConnection, pk=request.POST.get("server") or 0)
    how = "end" if request.POST.get("how") == "end" else "cancel"
    try:
        pid = int(request.POST.get("pid", ""))
    except ValueError:
        return JsonResponse({"ok": False, "error": "Give the process id of the session."}, status=400)
    function = "pg_terminate_backend" if how == "end" else "pg_cancel_backend"
    what = "Ended the session" if how == "end" else "Cancelled the query of the session"
    try:
        with pg.connect_rw(server) as conn:
            row = conn.execute(f"SELECT {function}(pid), usename, datname FROM pg_stat_activity "
                               "WHERE pid = %s AND pid <> pg_backend_pid()", (pid,)).fetchone()    # other users' rows are blanked for a limited login
    except PgError as error:
        record(request.user, f"session.{how}", server, f"{what} {pid} failed: {error}", ok=False)
        return JsonResponse({"ok": False, "error": str(error)}, status=400)
    if row is None:
        return JsonResponse({"ok": False, "error": f"Session {pid} is not there any more."}, status=404)
    if not row[0]:
        record(request.user, f"session.{how}", server, f"{what} {pid} failed: PostgreSQL did not signal it", ok=False)
        return JsonResponse({"ok": False, "error": f"PostgreSQL did not signal session {pid}."}, status=400)
    record(request.user, f"session.{how}", server, f"{what} {pid} ({row[1]} on {row[2]})")
    return JsonResponse({"ok": True, "message": f"{what} {pid}."})
