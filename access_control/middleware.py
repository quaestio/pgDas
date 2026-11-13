from django.contrib import auth, messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, get_object_or_404
from django.utils.deprecation import MiddlewareMixin
from .models import Installation
from .policy import membership_for, require
from . import network, runtime


class AccessMiddleware(MiddlewareMixin):
    def __call__(self, request):
        token = runtime.request_context.set(request)
        try:
            return super().__call__(request)
        finally:
            runtime.request_context.reset(token)

    JSON_PREFIXES = ("/browse/", "/monitor/", "/watch/", "/dashboard/", "/c/")

    def process_view(self, request, view, args, kwargs):
        """Django hands exceptions raised here straight to its error page, not to process_exception, so a refusal is
        recorded and answered here: JSON for the page scripts, Django's 403 page for everything else."""
        try:
            return self._check(request, view, args, kwargs)
        except PermissionDenied as error:
            return self._denied(request, error) or self._raise(error)

    @staticmethod
    def _raise(error):
        raise error

    JSON_NAMES = {"backup_databases", "backup_schemas"}

    def _address_refused(self, request, address):
        from audit.models import record
        record(request.user, "access.address_refused", None, f"{address or 'unknown address'} is not in the allowed list of the organization", ok=False)
        auth.logout(request)
        text = "Access from this address is not allowed for your account. Ask a Super User of your organization."
        if self._wants_json(request):
            return JsonResponse({"ok": False, "error": text, "signed_out": True}, status=403)
        messages.error(request, text)
        return redirect("login")

    def _wants_json(self, request):
        return (request.path.startswith(self.JSON_PREFIXES) or request.resolver_match.url_name in self.JSON_NAMES or request.content_type == "application/json"
                or request.headers.get("X-Requested-With") == "fetch")

    def _denied(self, request, error):
        """Record the refusal; the JSON answer for scripts, or None to let Django show its 403 page."""
        from audit.models import record
        record(request.user, "access.denied", None, request.path, ok=False)
        if self._wants_json(request):
            return JsonResponse({"ok": False, "error": str(error) or "Access denied."}, status=403)
        return None

    def _check(self, request, view, args, kwargs):
        name = request.resolver_match.url_name
        if name == "setup":
            return
        if not Installation.objects.filter(pk=1, completed=True).exists():
            return redirect("setup")
        if name in {"login", "logout"} or not request.user.is_authenticated:
            return
        member = membership_for(request.user)
        if not member:
            raise PermissionDenied("No active organization membership. Contact your Super User.")
        # Legacy UI guards now reflect the PGDAS role, not Django's staff bit.
        request.user.is_staff = member.role == "super_user"
        request.pgdas_member = member
        # An organization may list the addresses its people use PGDAS from. Super Users are never held to the list, so a wrong
        # list cannot lock everyone out. Everyone else is signed out the moment a request comes from an address outside it.
        if member.role != "super_user" and member.organization.allowed_ips.strip():
            address = network.client_ip(request)
            if not network.allowed(member.organization.allowed_ips, address):
                return self._address_refused(request, address)
        if name in {"dashboard", "dashboard_data"} and not runtime.is_admin():
            return redirect("server_list") if name == "dashboard" else JsonResponse({"ok": False, "error": "Server-wide dashboard is restricted to Super Users."}, status=403)
        admin_names = {"server_add", "server_edit", "server_delete", "server_test", "monitor", "monitor_data", "monitor_stop",
                       "monitor_features", "monitor_feature_change", "watch", "watch_data", "watch_sample", "watch_test_notify",
                       "backup_delete", "backup_restore", "schedule_save", "schedule_delete", "audit_reinsert", "audit_undo"}
        if name in admin_names:
            require(request.user, "servers_manage", organization=member.organization_id)
        server_id = request.GET.get("server") or request.POST.get("server")
        if server_id and not str(server_id).isdigit():
            raise Http404("Unknown server.")                       # not a server number: not found, not a crash
        if name in {"server_edit", "server_delete", "server_test", "explorer", "sql_runner", "write_preview", "write_execute", "sql_export", "maintenance_run"}:
            server_id = kwargs.get("pk")
        if server_id:
            get_object_or_404(runtime.servers(), pk=server_id)
        if name in {"backup_download", "backup_delete", "backup_restore"}:
            backup = get_object_or_404(runtime.backups(), pk=kwargs["pk"])
            if name == "backup_download":
                require(request.user, "backup_download", server=backup.server, database=backup.database)
        if name == "schedule_delete":
            from backups.models import BackupSchedule
            get_object_or_404(BackupSchedule, pk=kwargs["pk"], server__in=runtime.servers())
        if name in {"audit_json", "audit_reinsert", "audit_undo"}:
            entry = get_object_or_404(runtime.audits(), pk=kwargs["pk"])
            if not runtime.is_admin():
                require(request.user, "export", server=entry.server, database=(entry.payload or {}).get("database"))
        if name in {"backup_create", "backup_schemas"}:
            server = get_object_or_404(runtime.servers(), pk=server_id)
            database = request.POST.get("database") if name == "backup_create" else request.GET.get("database")
            if database or name == "backup_create":                  # no database chosen yet is an empty list of schemas, not a refusal
                require(request.user, "backup_create", server=server, database=database)

    def process_exception(self, request, exception):
        if isinstance(exception, PermissionDenied):
            return self._denied(request, exception)
