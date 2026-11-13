from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from audit.models import AuditEntry, record
from explorer import pg

from .forms import ServerForm
from .models import ServerConnection
from access_control import runtime
from access_control.models import OrganizationServer

staff_required = user_passes_test(lambda user: user.is_staff)


@login_required
def server_list(request):
    """Home page: the Object Browser. Servers is its top node; saved servers sit beneath it."""
    return render(request, "explorer/explorer.html", {
        "server_count": runtime.servers().count(),
        "user_count": get_user_model().objects.filter(is_active=True, pgdas_membership__organization=request.pgdas_member.organization).count(),
        "sql_runs": runtime.audits().filter(action="sql.run").count(),
        "preselect": request.GET.get("server", ""),
        # Opened onto a particular node (/?type=table&server=1&...) or the old /?server=1 address.
        "deep_link": bool(request.GET.get("server") or request.GET.get("type", "servers") != "servers"),
    })


@login_required
@staff_required
def server_add(request):
    form = ServerForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        server = form.save(commit=False)
        server.created_by = request.user
        server.save()
        OrganizationServer.objects.get_or_create(server=server, defaults={"organization": request.pgdas_member.organization})
        detail = f"{server.username}@{server.host}:{server.port}"
        record(request.user, "server.add", server, detail)
        messages.success(request, f"Server {server.name} saved.")
        return redirect("server_list")
    return render(request, "servers/form.html", {"form": form, "title": "Add server"})


@login_required
@staff_required
def server_edit(request, pk):
    server = get_object_or_404(ServerConnection, pk=pk)
    form = ServerForm(request.POST or None, instance=server)
    if request.method == "POST" and form.is_valid():
        form.save()
        detail = f"{server.username}@{server.host}:{server.port}"
        record(request.user, "server.edit", server, detail)
        messages.success(request, f"Server {server.name} updated.")
        return redirect("server_list")
    return render(request, "servers/form.html", {"form": form, "title": f"Edit {server.name}"})


@login_required
@staff_required
def server_delete(request, pk):
    server = get_object_or_404(ServerConnection, pk=pk)
    if request.method == "POST":
        record(request.user, "server.delete", server)
        server.delete()
        messages.success(request, f"Server {server.name} removed.")
        return redirect("server_list")
    return render(request, "servers/confirm_delete.html", {"server": server})


def _too_many_tests(request):
    from django.core.cache import cache
    from django.conf import settings
    key = f"pgdas-server-test:{request.user.pk}"                  # "Test" opens a connection to wherever the server points: not unlimited
    cache.add(key, 0, 600)
    return cache.incr(key) > settings.SERVER_TESTS_PER_10_MINUTES


TEST_SALT = "pgdas.server-draft-test"
DRAFT_FIELDS = ("name", "host", "port", "username", "password", "sslmode", "ssh_enabled", "ssh_host", "ssh_port", "ssh_username", "ssh_auth",
                "ssh_password", "ssh_key", "ssh_passphrase")


def _fingerprint(form):
    """A keyed hash of every detail that was tested: saving is allowed only for exactly what passed the test."""
    import hashlib
    import hmac
    import json
    from django.conf import settings
    payload = json.dumps({name: str(form.cleaned_data.get(name) or "") for name in DRAFT_FIELDS}, sort_keys=True).encode()
    return hmac.new(settings.SECRET_KEY.encode(), payload, hashlib.sha256).hexdigest()


def _field_errors(form):
    return {name: [str(e) for e in errors] for name, errors in form.errors.items()}


@login_required
@staff_required
@require_POST
def server_test_draft(request):
    """The Add server popup's Test button: connect with what was typed, saving nothing. A pass returns a token that lets Save go ahead."""
    from django.core import signing
    from django.http import JsonResponse
    from .tunnel import fingerprint
    form = ServerForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": _field_errors(form), "error": "Some details need attention."}, status=400)
    if _too_many_tests(request):
        return JsonResponse({"ok": False, "error": "Too many connection tests in a short time. Try again in a few minutes."}, status=429)
    server = form.save(commit=False)
    server.pgdas_draft = True
    try:
        version = pg.test_connection(server)
    except pg.PgError as error:
        return JsonResponse({"ok": False, "error": str(error)})
    token = signing.dumps({"fp": _fingerprint(form), "key": server.ssh_host_key if server.ssh_enabled else ""}, salt=TEST_SALT)
    return JsonResponse({"ok": True, "message": f"Connected, PostgreSQL {version}.", "token": token,
                         "ssh_fingerprint": fingerprint(server.ssh_host_key) if server.ssh_enabled else ""})


@login_required
@staff_required
@require_POST
def server_create(request):
    """The Add server popup's Save button: saves only what passed the test (same details, tested in the last 15 minutes)."""
    from django.core import signing
    from django.http import JsonResponse
    form = ServerForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": _field_errors(form), "error": "Some details need attention."}, status=400)
    try:
        tested = signing.loads(request.POST.get("test_token", ""), salt=TEST_SALT, max_age=900)
    except signing.BadSignature:
        return JsonResponse({"ok": False, "error": "Test the connection first: a server is saved only after a successful test."}, status=400)
    if tested.get("fp") != _fingerprint(form):
        return JsonResponse({"ok": False, "error": "The details changed after the test. Press Test again."}, status=400)
    server = form.save(commit=False)
    server.created_by = request.user
    server.ssh_host_key = tested.get("key", "") if server.ssh_enabled else ""
    server.save()
    OrganizationServer.objects.get_or_create(server=server, defaults={"organization": request.pgdas_member.organization})
    via = f" via SSH {server.ssh_username}@{server.ssh_host}:{server.ssh_port}" if server.ssh_enabled else ""
    record(request.user, "server.add", server, f"{server.username}@{server.host}:{server.port}{via}")
    messages.success(request, f"Server {server.name} saved.")
    return JsonResponse({"ok": True, "message": f"Server {server.name} saved."})


@login_required
@require_POST
def server_test(request, pk):
    server = get_object_or_404(ServerConnection, pk=pk)
    if _too_many_tests(request):
        messages.error(request, "Too many connection tests in a short time. Try again in a few minutes.")
        return redirect("server_list")
    try:
        version = pg.test_connection(server)
    except pg.PgError as error:
        messages.error(request, f"{server.name}: connection failed — {error}")
    else:
        messages.success(request, f"{server.name}: connected, PostgreSQL {version}.")
    return redirect("server_list")
