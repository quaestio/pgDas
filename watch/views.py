import json
from datetime import timedelta

from access_control import mode, runtime

from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.cache import cache
from django.utils import timezone
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from audit.models import record
from explorer.pg import PgError
from servers import ids
from servers.models import ServerConnection

from . import alerts, health, metrics, notify, sampling, series, server_settings
from .models import MetricSample

staff_required = user_passes_test(lambda user: user.is_staff)


def _grants(user):
    """The databases a non-Super-User may read, as {server pk: [database names]} (what the employee versions of the dashboards and the health page may show)."""
    from access_control.models import DatabaseGrant
    from access_control.policy import membership_for
    member = membership_for(user)
    found = {}
    if member is not None:
        for grant in DatabaseGrant.objects.filter(membership=member, read=True).order_by("database"):
            found.setdefault(grant.server_id, []).append(grant.database)
    return found


def monitor_required(view):
    """Super Users, and anyone who may read at least one database (they see only their own databases: see series.restricted_view)."""
    from functools import wraps
    from django.core.exceptions import PermissionDenied

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not (request.user.is_staff or _grants(request.user)):
            raise PermissionDenied("Monitoring is for Super Users and for people with access to a database.")
        return view(request, *args, **kwargs)
    return wrapped
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
                         "channels": notify.channels(server)})


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
    chosen = notify.channels_for_organization(request.pgdas_member.organization)
    if not (chosen["webhook_url"] or chosen["emails"]):
        text = ("No channel is set up for your organization. Open Alert notifications (Monitoring, Alerts & trends) and add an email address or a webhook.")
        if not mode.is_shared():
            text += " Or set PGDAS_ALERT_WEBHOOK_URL, or PGDAS_ALERT_EMAIL_TO with PGDAS_EMAIL_HOST, in .env and restart PGDAS."
        return JsonResponse({"ok": False, "error": text}, status=400)
    done, failed = notify.send("PGDAS: test notification", f"This is a test from PGDAS, sent by {request.user.username}. Alerts will arrive like this.", chosen)
    record(request.user, "alert.test", None, f"Test notification: sent on {', '.join(done) or 'nothing'}" +
           "".join(f"; failed on {c}: {r}" for c, r in failed), ok=bool(done) and not failed)
    if failed:
        return JsonResponse({"ok": False, "error": "; ".join(f"{c}: {r}" for c, r in failed), "sent": done}, status=502)
    return JsonResponse({"ok": True, "message": "Test sent on " + " and ".join(done) + "."})

@login_required
@monitor_required
def dashboards_page(request):
    """Charts of everything PostgreSQL reports about itself, sampled over time (see series.py). Super Users of an organization see their own servers."""
    servers = list(runtime.servers().order_by("name"))
    chosen = next((s for s in servers if str(s.pk) == str(request.GET.get("server") or "")), servers[0] if servers else None)
    selected = chosen.sid if chosen else ""
    return render(request, "watch/dashboards.html", {"servers": servers, "selected": selected, "ranges": list(series.RANGES), "default_range": "6h",
                                                      "interval": settings.WATCH_INTERVAL_SECONDS, "keep_days": settings.WATCH_METRIC_KEEP_DAYS,
                                                      "restricted": not request.user.is_staff})


@login_required
@monitor_required
def dashboards_data(request):
    """The JSON behind the page. Short-lived cache: many people (and tabs) looking at the same server cost the database one query set per 20 seconds."""
    server = get_object_or_404(runtime.servers(), pk=request.GET.get("server") or 0)
    window = request.GET.get("range") if request.GET.get("range") in series.RANGES else "6h"
    database = (request.GET.get("db") or "")[:200]
    restricted = not request.user.is_staff
    if restricted:                                       # an employee sees one database they may read, never the server as a whole
        readable = _grants(request.user).get(server.pk, [])
        if not database:
            return JsonResponse({"ok": True, "scope": "database", "need_database": True, "databases": readable, "server": {"id": server.sid, "name": server.name},
                                 "database": "", "range_key": window, "panels": {}, "tiles": {}, "tables": {}, "range": {}})
        if database not in readable:
            return JsonResponse({"ok": False, "error": f"PGDAS blocked this: you have no access to {database!r} on {server.name!r}. Ask a Super User to give you access."}, status=403)
    key = f"pgdas-dash:{server.pk}:{cache.get(f'pgdas-dash-v:{server.pk}', 0)}:{window}:{database}:{int(restricted)}"
    payload = cache.get(key)
    if payload is None:
        data = series.dashboard(server, series.RANGES[window] / 3600, database, restricted=restricted)
        names = _grants(request.user).get(server.pk, []) if restricted else list(
            MetricSample.objects.filter(server=server, taken_at__gte=timezone.now() - timedelta(hours=series.RANGES[window] / 3600))
            .exclude(database="").order_by().values_list("database", flat=True).distinct())
        payload = {"ok": True, "server": {"id": server.sid, "name": server.name}, "databases": sorted(names), "range_key": window, "database": database, **data}
        cache.set(key, payload, 20)
    return JsonResponse({**payload, "can_edit_settings": runtime.is_admin(request.user)})          # who is looking is not part of the cached answer


def _setting_request(request):
    """(server, name, value, reset) of a change to a server setting, or a JsonResponse that refuses it. Super Users only."""
    if not runtime.is_admin(request.user):
        return JsonResponse({"ok": False, "error": "Only a Super User may change server settings."}, status=403)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"ok": False, "error": "That request was not understood."}, status=400)
    number = ids.decode(body.get("server"))                                # the page sends the token; an old plain number is understood too
    server = runtime.servers().filter(pk=number).first() if number else None
    if server is None:
        return JsonResponse({"ok": False, "error": "That server was not found."}, status=404)
    return server, str(body.get("name") or ""), body.get("value"), bool(body.get("reset")), body


@login_required
@staff_required
@require_POST
def setting_preview(request):
    asked = _setting_request(request)
    if isinstance(asked, JsonResponse):
        return asked
    server, name, value, reset, _ = asked
    try:
        return JsonResponse({"ok": True, **server_settings.preview(server, name, value, reset)})
    except PgError as error:
        return JsonResponse({"ok": False, "error": str(error)}, status=400)


@login_required
@staff_required
@require_POST
def setting_apply(request):
    asked = _setting_request(request)
    if isinstance(asked, JsonResponse):
        return asked
    server, name, value, reset, body = asked
    try:
        result = server_settings.execute(server, name, value, reset, body.get("fingerprint"), body.get("confirm"), request.user)
    except PgError as error:
        return JsonResponse({"ok": False, "error": str(error)}, status=400)
    cache.set(f"pgdas-dash-v:{server.pk}", int(timezone.now().timestamp()))
    cache.delete(f"pgdas-live:{server.pk}")
    return JsonResponse({"ok": True, **result})

@login_required
@staff_required
def dashboards_live(request):
    """The right-now reading behind the live strip. Cached for a few seconds per server: ten people watching one server cost it one reading, not ten."""
    server = get_object_or_404(runtime.servers(), pk=request.GET.get("server") or 0)
    key = f"pgdas-live:{server.pk}"
    reading = cache.get(key)
    if reading is None:
        try:
            reading = metrics.live(server)
        except PgError as error:
            return JsonResponse({"ok": False, "error": str(error)}, status=502)
        cache.set(key, reading, 8)
    return JsonResponse({"ok": True, "reading": reading})

# ---- an organization's alert channels ------------------------------------------------------------------------------------------------------------

@login_required
@staff_required
def alert_settings(request):
    """Alert notifications of the signed-in Super User's organization: recipients (confirmed by their owners) and a webhook."""
    from django.contrib import messages
    from django.shortcuts import redirect
    from access_control.models import AlertRecipient
    from . import recipients
    organization = request.pgdas_member.organization
    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            if action == "add":
                recipient, outcome = recipients.add_recipient(organization, request.POST.get("email", ""))
                messages.success(request, {"confirmed": f"{recipient.email} belongs to a verified member, so it is confirmed and will receive alerts.",
                                           "waiting": f"A confirmation link was sent to {recipient.email}. It receives alerts after its owner opens the link.",
                                           "unsent": f"{recipient.email} was added, but the confirmation email could not be sent just now (see the service log). It receives alerts only "
                                                     "after its owner confirms: press Send the link again later.",
                                           "exists": f"{recipient.email} is already in the list."}[outcome])
            elif action == "remove":
                AlertRecipient.objects.filter(organization=organization, pk=request.POST.get("recipient") or 0).delete()
                messages.success(request, "Recipient removed.")
            elif action == "resend":
                recipient = get_object_or_404(AlertRecipient, organization=organization, pk=request.POST.get("recipient") or 0, verified=False)
                sent = recipients.send_confirmation(recipient)
                (messages.success if sent else messages.warning)(request, "The confirmation link was sent again." if sent else
                                                                 "The link could not be sent now: one went out a moment ago (try again in ten minutes), or email is not working (see the service log).")
            elif action == "webhook":
                url, _ = recipients.check_webhook(request.POST.get("webhook", ""))
                organization.alert_webhook_url = url
                organization.save(update_fields=["alert_webhook_url"])
                record(request.user, "alert.webhook", None, f"Webhook of the organization set (to {recipients.webhook_host(url)})", payload={"organization": organization.pk})
                messages.success(request, f"Webhook saved (host {recipients.webhook_host(url)}). Press Send a test to check it.")
            elif action == "webhook_remove":
                organization.alert_webhook_url = ""
                organization.save(update_fields=["alert_webhook_url"])
                record(request.user, "alert.webhook", None, "Webhook of the organization removed", payload={"organization": organization.pk})
                messages.success(request, "Webhook removed.")
        except (ValueError, recipients.WebhookRefused) as error:
            messages.error(request, str(error))
        return redirect("alert_settings")
    chosen = notify.channels_for_organization(organization)
    return render(request, "watch/alert_settings.html", {
        "organization": organization, "recipients": AlertRecipient.objects.filter(organization=organization),
        "webhook_host": recipients.webhook_host(organization.alert_webhook_url), "source": chosen["source"], "shared": mode.is_shared(),
        "email_ready": bool(settings.EMAIL_HOST), "limit": settings.ALERT_MAX_RECIPIENTS, "per_hour": settings.ALERT_MAX_PER_HOUR})


def _recipient_page(request, token, salt, max_age, title, button, action):
    from django.shortcuts import redirect
    from . import recipients
    recipient = recipients.load(token, salt, max_age)
    if recipient is None:
        return render(request, "watch/alert_link.html", {"bad": True, "title": title}, status=400)
    if request.method == "POST":
        action(recipient)
        return render(request, "watch/alert_link.html", {"done": True, "title": title, "recipient": recipient})
    return render(request, "watch/alert_link.html", {"title": title, "button": button, "recipient": recipient})


def alert_confirm(request, token):
    """The link emailed to a new recipient: it only shows what is being asked, and the button confirms (so a mail scanner opening the link changes nothing)."""
    from . import recipients

    def confirm(recipient):
        recipient.verified = True
        recipient.save(update_fields=["verified"])
    return _recipient_page(request, token, recipients.CONFIRM_SALT, recipients.CONFIRM_DAYS * 86400, "Receive alerts", "Confirm", confirm)


def alert_stop(request, token):
    """The link at the end of every alert email: removes the address."""
    from . import recipients
    return _recipient_page(request, token, recipients.STOP_SALT, None, "Stop alerts", "Stop sending alerts to this address", lambda r: r.delete())

# ---- table and index health (on demand) ------------------------------------------------------------------------------------------------------------

@login_required
@monitor_required
def health_page(request):
    servers = list(runtime.servers().order_by("name"))
    chosen = next((s for s in servers if str(s.pk) == str(request.GET.get("server") or "")), servers[0] if servers else None)
    return render(request, "watch/health.html", {"servers": servers, "selected": chosen.sid if chosen else "", "limits": (50, 150, 300), "restricted": not request.user.is_staff})


@login_required
@monitor_required
def health_databases(request):
    """The databases of a server, for the picker (cached for a minute: the page asks again whenever the server changes). An employee gets only their own."""
    server = get_object_or_404(runtime.servers(), pk=request.GET.get("server") or 0)
    if not request.user.is_staff:
        return JsonResponse({"ok": True, "databases": _grants(request.user).get(server.pk, [])})
    key = f"pgdas-health-dbs:{server.pk}"
    names = cache.get(key)
    if names is None:
        from explorer import pg
        try:
            with pg.connect(server) as conn:
                names = [d["name"] for d in pg.list_databases(conn)]
        except PgError as error:
            return JsonResponse({"ok": False, "error": str(error)}, status=502)
        cache.set(key, names, 60)
    return JsonResponse({"ok": True, "databases": names})


@login_required
@monitor_required
def health_data(request):
    """Tables and indexes of one database with what is wrong and how to fix it. Read live, so it is cached for a minute per (server, database, size of list):
    asking again and again costs the monitored server nothing."""
    server = get_object_or_404(runtime.servers(), pk=request.GET.get("server") or 0)
    database = (request.GET.get("db") or "").strip()
    if not database:
        return JsonResponse({"ok": False, "error": "Choose a database."}, status=400)
    if not request.user.is_staff and database not in _grants(request.user).get(server.pk, []):
        return JsonResponse({"ok": False, "error": f"PGDAS blocked this: you have no access to {database!r} on {server.name!r}. Ask a Super User to give you access."}, status=403)
    try:
        limit = int(request.GET.get("limit") or 150)
    except ValueError:
        limit = 150
    limit = limit if limit in (50, 150, 300) else 150
    key = f"pgdas-health:{server.pk}:{database}:{limit}"
    payload = cache.get(key)
    if payload is None or request.GET.get("fresh"):
        try:
            data = health.collect(server, database, limit)
        except PgError as error:
            return JsonResponse({"ok": False, "error": str(error)}, status=502)
        payload = {"ok": True, "server": {"id": server.sid, "name": server.name}, **data}
        cache.set(key, payload, 60)
    from access_control.policy import permits
    can_fix = request.user.is_staff or permits(request.user, "maintenance", server=server, database=database)      # who is looking is not part of the cached answer
    return JsonResponse({**payload, "can_maintain": can_fix})


@login_required
@monitor_required
@require_POST
def health_bloat(request):
    """Exact dead space of one table (pgstattuple), on request: Super Users, and people with the Maintenance permission on that database."""
    try:
        body = json.loads(request.body or b"{}")
        number = ids.decode(body.get("server"))
        database, schema, table = (str(body.get(k) or "") for k in ("db", "schema", "table"))
        if not (number and database and schema and table):
            raise ValueError
        server = get_object_or_404(runtime.servers(), pk=number)
    except (ValueError, AttributeError):
        return JsonResponse({"ok": False, "error": "That request was not understood."}, status=400)
    from access_control.policy import require
    require(request.user, "maintenance", server=server, database=database)
    try:
        result = health.bloat(server, database, schema, table)
    except PgError as error:
        return JsonResponse({"ok": False, "error": str(error)}, status=400)
    record(request.user, "health.measure", server, f"{schema}.{table}", payload={"database": database})
    return JsonResponse({"ok": True, **result})
