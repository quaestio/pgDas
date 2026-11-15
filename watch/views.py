from access_control import runtime
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.cache import cache
from django.utils import timezone
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from audit.models import record
from explorer.pg import PgError
from servers.models import ServerConnection

from . import alerts, notify, sampling, series
from .models import MetricSample

staff_required = user_passes_test(lambda user: user.is_staff)
PERIODS = (1, 7, 30, 90)


@login_required
@staff_required
def watch_page(request):
    return render(request, "watch/page.html", {"servers": runtime.servers(), "periods": PERIODS})


@login_required
@staff_required
def watch_data(request):
    server = get_object_or_404(ServerConnection, pk=request.GET.get("server") or 0)
    try:
        days = int(request.GET.get("days") or 7)
    except ValueError:
        days = 7
    days = days if days in PERIODS else 7
    return JsonResponse({"ok": True, "alerts": alerts.evaluate(server), "series": sampling.series(server, days), "days": days,
                         "channels": notify.channels()})


@login_required
@staff_required
@require_POST
def watch_sample(request):
    """Take a size sample of one server now (the same thing `manage.py run_watch` does for all of them)."""
    server = get_object_or_404(ServerConnection, pk=request.POST.get("server") or 0)
    try:
        count = sampling.sample(server)
    except PgError as error:
        return JsonResponse({"ok": False, "error": str(error)}, status=502)
    cache.set(f"pgdas-dash-v:{server.pk}", int(timezone.now().timestamp()))      # the charts for this server should show the new sample at once
    record(request.user, "watch.sample", server, f"Sampled {count} database{'s' if count != 1 else ''}")
    return JsonResponse({"ok": True, "message": f"Sampled {count} database{'s' if count != 1 else ''}."})


@login_required
@staff_required
@require_POST
def watch_test_notify(request):
    """Send a test message on every channel that is set up, so the settings can be checked without waiting for a real alert."""
    if not notify.channels()["webhook"] and not notify.channels()["email"]:
        return JsonResponse({"ok": False, "error": "No channel is set up. Set PGDAS_ALERT_WEBHOOK_URL, or PGDAS_ALERT_EMAIL_TO with "
                                                   "PGDAS_EMAIL_HOST, in .env and restart PGDAS."}, status=400)
    done, failed = notify.send("PGDAS: test notification", f"This is a test from PGDAS, sent by {request.user.username}. Alerts will arrive like this.")
    record(request.user, "alert.test", None, f"Test notification: sent on {', '.join(done) or 'nothing'}" +
           "".join(f"; failed on {c}: {r}" for c, r in failed), ok=bool(done) and not failed)
    if failed:
        return JsonResponse({"ok": False, "error": "; ".join(f"{c}: {r}" for c, r in failed), "sent": done}, status=502)
    return JsonResponse({"ok": True, "message": "Test sent on " + " and ".join(done) + "."})

@login_required
@staff_required
def dashboards_page(request):
    """Charts of everything PostgreSQL reports about itself, sampled over time (see series.py). Super Users of an organization see their own servers."""
    servers = list(runtime.servers().order_by("name"))
    selected = request.GET.get("server") or (servers[0].pk if servers else "")
    return render(request, "watch/dashboards.html", {"servers": servers, "selected": str(selected), "ranges": list(series.RANGES), "default_range": "6h",
                                                      "interval": settings.WATCH_INTERVAL_SECONDS, "keep_days": settings.WATCH_METRIC_KEEP_DAYS})


@login_required
@staff_required
def dashboards_data(request):
    """The JSON behind the page. Short-lived cache: many people (and tabs) looking at the same server cost the database one query set per 20 seconds."""
    server = get_object_or_404(runtime.servers(), pk=request.GET.get("server") or 0)
    window = request.GET.get("range") if request.GET.get("range") in series.RANGES else "6h"
    database = (request.GET.get("db") or "")[:200]
    key = f"pgdas-dash:{server.pk}:{cache.get(f'pgdas-dash-v:{server.pk}', 0)}:{window}:{database}"
    payload = cache.get(key)
    if payload is None:
        data = series.dashboard(server, series.RANGES[window] / 3600, database)
        names = list(MetricSample.objects.filter(server=server, taken_at__gte=timezone.now() - timedelta(hours=series.RANGES[window] / 3600))
                     .exclude(database="").order_by().values_list("database", flat=True).distinct())
        payload = {"ok": True, "server": {"id": server.pk, "name": server.name}, "databases": sorted(names), "range_key": window, "database": database, **data}
        cache.set(key, payload, 20)
    return JsonResponse(payload)
