from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from audit.models import AuditEntry, record
from explorer import pg

from .forms import ServerForm
from .models import ServerConnection

staff_required = user_passes_test(lambda user: user.is_staff)


@login_required
def server_list(request):
    return render(request, "servers/list.html", {
        "servers": ServerConnection.objects.all(),
        "user_count": get_user_model().objects.filter(is_active=True).count(),
        "sql_runs": AuditEntry.objects.filter(action="sql.run").count(),
    })


@login_required
@staff_required
def server_add(request):
    form = ServerForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        server = form.save(commit=False)
        server.created_by = request.user
        server.save()
        record(request.user, "server.add", server, f"{server.username}@{server.host}:{server.port}/{server.database}")
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
        record(request.user, "server.edit", server, f"{server.username}@{server.host}:{server.port}/{server.database}")
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


@login_required
@require_POST
def server_test(request, pk):
    server = get_object_or_404(ServerConnection, pk=pk)
    try:
        version = pg.test_connection(server)
    except pg.PgError as error:
        messages.error(request, f"{server.name}: connection failed — {error}")
    else:
        messages.success(request, f"{server.name}: connected, PostgreSQL {version}.")
    return redirect("server_list")
