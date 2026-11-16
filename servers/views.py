from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from audit.models import AuditEntry, record
from explorer import pg

from . import unlock
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
                "ssh_password", "ssh_key", "ssh_passphrase", "ssh_prompt_passphrase", "comments", "kerberos", "krbsrvname", "database", "save_password")
# What decides whether an edit needs a new test: anything that changes how PGDAS connects, or a secret that was typed.
CONNECTION_FIELDS = ("host", "port", "database", "username", "kerberos", "krbsrvname", "sslmode", "save_password", "ssh_enabled", "ssh_host", "ssh_port",
                     "ssh_username", "ssh_auth", "ssh_prompt_passphrase")
SECRET_FIELDS = ("password", "ssh_password", "ssh_key", "ssh_passphrase")


def _fingerprint(form):
    """A keyed hash of every detail that was tested: saving is allowed only for exactly what passed the test."""
    import hashlib
    import hmac
    import json
    from django.conf import settings
    payload = json.dumps({"pk": str(form.instance.pk or ""), **{name: str(form.cleaned_data.get(name) or "") for name in DRAFT_FIELDS}}, sort_keys=True).encode()
    return hmac.new(settings.SECRET_KEY.encode(), payload, hashlib.sha256).hexdigest()


def _form_for(request):
    """The popup's form. With `server_id` it is an edit: the form starts from a copy of the saved server, so a blank password or key means keep it."""
    from copy import copy
    server_id = request.POST.get("server_id", "")
    instance = copy(get_object_or_404(runtime.servers(), pk=int(server_id))) if server_id.isdigit() else None
    return ServerForm(request.POST, instance=instance)


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
    form = _form_for(request)
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
    typed_passphrase = getattr(server, "_typed_ssh_passphrase", None) if server.ssh_enabled and server.ssh_prompt_passphrase else None
    typed_password = getattr(server, "_typed_password", None) if server.needs_password_unlock else None
    server.save()
    if typed_passphrase or typed_password:
        unlock.unlock(server, typed_passphrase, request.user, password=typed_password)       # just proven by the test: the server starts unlocked
    OrganizationServer.objects.get_or_create(server=server, defaults={"organization": request.pgdas_member.organization})
    via = f" via SSH {server.ssh_username}@{server.ssh_host}:{server.ssh_port}" if server.ssh_enabled else ""
    record(request.user, "server.add", server, f"{server.username}@{server.host}:{server.port}{via}")
    messages.success(request, f"Server {server.name} saved.")
    return JsonResponse({"ok": True, "message": f"Server {server.name} saved."})


@login_required
@staff_required
@require_GET
def server_json(request, pk):
    """What the Edit popup shows: every setting of the server except its secrets (which are only reported as saved or not)."""
    from django.http import JsonResponse
    from .tunnel import fingerprint
    server = get_object_or_404(runtime.servers(), pk=pk)
    return JsonResponse({"ok": True, "id": server.sid, "values": {
        "name": server.name, "comments": server.comments, "host": server.host, "port": server.port, "database": server.database if server.database != "postgres" else "",
        "username": server.username, "sslmode": server.sslmode, "kerberos": server.kerberos, "krbsrvname": server.krbsrvname,
        "save_password": server.save_password, "ssh_enabled": server.ssh_enabled, "ssh_host": server.ssh_host, "ssh_port": server.ssh_port,
        "ssh_username": server.ssh_username, "ssh_auth": server.ssh_auth, "ssh_prompt_passphrase": server.ssh_prompt_passphrase},
        "saved": {"password": bool(server.password_encrypted), "ssh_secret": bool(server.ssh_secret_encrypted), "ssh_passphrase": bool(server.ssh_passphrase_encrypted),
                  "ssh_host_key": fingerprint(server.ssh_host_key)}})


@login_required
@staff_required
@require_POST
def server_update(request, pk):
    """The Edit popup's Save. A new test is needed only when something that changes how PGDAS connects was changed (or a secret was typed);
    renaming a server or editing its comments needs none."""
    from django.core import signing
    from django.http import JsonResponse
    saved = get_object_or_404(runtime.servers(), pk=pk)
    post = request.POST.copy()
    post["server_id"] = str(saved.pk)
    request.POST = post
    form = _form_for(request)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": _field_errors(form), "error": "Some details need attention."}, status=400)
    needs_test = any(form.cleaned_data.get(name) for name in SECRET_FIELDS) or any(name in form.changed_data for name in CONNECTION_FIELDS)
    tested = {}
    if needs_test:
        try:
            tested = signing.loads(request.POST.get("test_token", ""), salt=TEST_SALT, max_age=900)
        except signing.BadSignature:
            return JsonResponse({"ok": False, "error": "You changed how PGDAS connects: press Test connection first. It is saved only after a successful test."}, status=400)
        if tested.get("fp") != _fingerprint(form):
            return JsonResponse({"ok": False, "error": "The details changed after the test. Press Test again."}, status=400)
    server = form.save(commit=False)
    if server.ssh_enabled and not server.ssh_host_key and tested.get("key"):
        server.ssh_host_key = tested["key"]
    typed_passphrase = getattr(server, "_typed_ssh_passphrase", None) if server.ssh_enabled and server.ssh_prompt_passphrase else None
    typed_password = getattr(server, "_typed_password", None) if server.needs_password_unlock else None
    server.save()
    if typed_passphrase or typed_password:
        unlock.unlock(server, typed_passphrase, request.user, password=typed_password)
    via = f" via SSH {server.ssh_username}@{server.ssh_host}:{server.ssh_port}" if server.ssh_enabled else ""
    record(request.user, "server.edit", server, f"{server.username}@{server.host}:{server.port}{via}")
    messages.success(request, f"Server {server.name} updated.")
    return JsonResponse({"ok": True, "message": f"Server {server.name} updated."})

@login_required
@staff_required
@require_POST
def server_unlock(request, pk):
    """Unlock a server whose password and/or SSH key passphrase are not saved. What is typed is tried with a real connection; the secrets are then
    kept (encrypted) for a few hours and deleted."""
    from copy import copy
    from django.http import JsonResponse
    server = get_object_or_404(ServerConnection, pk=pk)
    if not server.asks_for_secret:
        return JsonResponse({"ok": False, "error": "This server does not ask for a password or passphrase."}, status=400)
    password, passphrase = request.POST.get("password", ""), request.POST.get("passphrase", "")
    asks_password = server.needs_password_unlock
    asks_passphrase = server.ssh_enabled and server.ssh_prompt_passphrase
    if asks_password and not password:
        return JsonResponse({"ok": False, "error": "Enter the password of this server."}, status=400)
    if asks_passphrase and not passphrase:
        return JsonResponse({"ok": False, "error": "Enter the passphrase of the SSH key."}, status=400)
    candidate = copy(server)
    candidate.pgdas_draft = True
    if asks_password:
        candidate._typed_password = password
    if asks_passphrase:
        candidate._typed_ssh_passphrase = passphrase
    try:
        pg.test_connection(candidate)                              # the real thing: tunnel (if any), sign-in, one query
    except pg.PgError as error:
        record(request.user, "server.unlock", server, "unlock refused: the connection failed", ok=False)
        return JsonResponse({"ok": False, "error": str(error)}, status=400)
    expires = unlock.unlock(server, passphrase if asks_passphrase else None, request.user, password=password if asks_password else None)
    record(request.user, "server.unlock", server, f"unlocked until {expires:%Y-%m-%d %H:%M} UTC")
    return JsonResponse({"ok": True, "message": f"{server.name} is unlocked until {expires:%H:%M} UTC.", "until": expires.isoformat()})

@login_required
@staff_required
@require_POST
def server_lock(request, pk):
    from django.http import JsonResponse
    server = get_object_or_404(ServerConnection, pk=pk)
    unlock.lock(server)
    record(request.user, "server.lock", server, "SSH key locked")
    return JsonResponse({"ok": True, "message": f"{server.name} is locked."})


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
