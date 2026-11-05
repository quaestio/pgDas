import os
import secrets
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from access_control.models import Organization, Membership, Installation, DatabaseGrant
from access_control.policy import membership_for, require, visible_servers, GRANT_ACTIONS
from access_control.services import assign_database, revoke_database
from audit.models import record
from servers import crypto
from access_control import network
from .forms import EmployeeForm, GrantForm, NewOrganizationForm, OrganizationSettingsForm, SetupForm


def administrator(request):
    member = membership_for(request.user)
    if not member:
        raise PermissionDenied
    require(request.user, "employees_manage", organization=member.organization_id)
    return member


def setup(request):
    if Installation.objects.filter(pk=1, completed=True).exists():
        raise Http404
    form = SetupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        token = os.getenv("PGDAS_SETUP_TOKEN", "")
        path = Path(settings.BASE_DIR) / "data" / "installation.token"
        if not token and path.exists():
            token = path.read_text().strip()
        if not token or not secrets.compare_digest(token, form.cleaned_data["installation_token"]):
            form.add_error("installation_token", "Invalid installation token. Run manage.py installation_token on the host.")
        else:
            with transaction.atomic():
                installation = Installation.objects.select_for_update().get(pk=1)
                if installation.completed:
                    raise Http404
                org = Organization.objects.create(name=form.cleaned_data["organization"])
                user = form.save(commit=False)
                user.first_name = form.cleaned_data["name"]
                user.is_active = True
                user.is_superuser = user.is_staff = True
                user.set_password(form.cleaned_data["password"])
                user.save()
                Membership.objects.create(user=user, organization=org, role="super_user")
                installation.completed = True
                installation.save(update_fields=["completed"])
                record(user, "installation.complete", None, "Organization and initial Super User created")
            login(request, user, backend="accounts.backends.EmployeeBackend")
            return redirect("employee_list")
    return render(request, "accounts/setup.html", {"form": form})


@login_required
def employees(request):
    member = administrator(request)
    people = Membership.objects.filter(organization_id=member.organization_id, user__deleted_at__isnull=True).select_related("user")
    return render(request, "accounts/list.html", {"people": people, "organization": member.organization})


@login_required
def employee_edit(request, pk=None):
    admin = administrator(request)
    member = get_object_or_404(Membership, user_id=pk, organization=admin.organization, user__deleted_at__isnull=True) if pk else None
    user = member.user if member else get_user_model()()
    form = EmployeeForm(request.POST or None, instance=user, initial={"name": user.get_full_name(), "role": member.role if member else "viewer"})
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=admin.organization_id)
            administrator(request)
            if member:
                member.refresh_from_db()
            role = form.cleaned_data["role"]
            if member and member.role == "super_user" and (role != "super_user" or not form.cleaned_data["is_active"]) and not Membership.objects.filter(
                    organization=admin.organization, role="super_user", user__is_active=True).exclude(pk=member.pk).exists():
                form.add_error(None, "Keep at least one active Super User in the organization.")
            else:
                user = form.save(commit=False)
                user.first_name = form.cleaned_data["name"]
                user.last_name = ""
                user.is_staff = user.is_superuser = role == "super_user"
                if form.cleaned_data.get("password"):
                    user.set_password(form.cleaned_data["password"])
                user.save()
                Membership.objects.update_or_create(user=user, defaults={"organization": admin.organization, "role": role})
                record(request.user, "employee.save", None, f"Employee #{user.pk} saved; role={role}; active={user.is_active}")
                if user.pk == request.user.pk:
                    update_session_auth_hash(request, user)
                messages.success(request, "Employee saved.")
                if request.headers.get("X-Employee-Modal") == "1":
                    return JsonResponse({"ok": True})
                return redirect("employee_list")
    if request.headers.get("X-Employee-Modal") == "1":
        return render(request, "accounts/_employee_form.html", {"form": form, "title": "Edit employee" if pk else "Add employee"})
    return render(request, "accounts/form.html", {"form": form, "title": "Edit employee" if pk else "Add employee"})


@login_required
def employee_delete(request, pk):
    admin = administrator(request)
    member = get_object_or_404(Membership, user_id=pk, organization=admin.organization, user__deleted_at__isnull=True)
    error = None
    if request.method == "POST":
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=admin.organization_id)
            administrator(request)
            member.refresh_from_db()
            if member.role == "super_user" and not Membership.objects.filter(
                    organization=admin.organization, role="super_user", user__is_active=True, user__deleted_at__isnull=True).exclude(pk=member.pk).exists():
                error = "Keep at least one active Super User in the organization."
            elif request.POST.get("confirm") != "delete":
                error = "Please confirm deleting this employee."
            else:
                user = member.user
                user.is_active = False
                user.deleted_at = timezone.now()
                user.save(update_fields=["is_active", "deleted_at"])
                member.database_grants.all().delete()
                record(request.user, "employee.delete", None, f"Employee #{user.pk} removed; account disabled and audit history retained")
                messages.success(request, "Employee deleted. Access is disabled; audit history is retained.")
                if request.headers.get("X-Employee-Modal") == "1":
                    return JsonResponse({"ok": True})
                return redirect("employee_list")
    return render(request, "accounts/_employee_delete.html", {"employee": member.user, "error": error})


@login_required
def permissions(request, pk):
    admin = administrator(request)
    employee = get_object_or_404(Membership, user_id=pk, organization=admin.organization)
    grant = None
    if request.GET.get("grant"):
        grant = get_object_or_404(DatabaseGrant, pk=request.GET["grant"], membership=employee)
    instance = grant or DatabaseGrant(membership=employee)
    form = GrantForm(request.POST or None, instance=instance)
    form.fields["server"].queryset = visible_servers(request.user)
    if grant:
        form.fields["server"].disabled = True
        form.fields["database"].disabled = True
    if employee.role == "viewer":
        for action in GRANT_ACTIONS - {"read", "export"}:
            form.fields[action].disabled = True
            form.initial[action] = False
    elif grant is None:
        form.initial.update(insert=True, update=True)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                saved = assign_database(request.user, employee, form.cleaned_data["server"], form.cleaned_data["database"],
                                        {action: form.cleaned_data[action] for action in GRANT_ACTIONS})
                username = form.cleaned_data["execution_username"]
                if saved.execution_username != username and not form.cleaned_data["password"]:
                    saved.execution_password = ""
                saved.execution_username = username
                if form.cleaned_data["password"]:
                    saved.execution_password = crypto.encrypt(form.cleaned_data["password"])
                saved.save(update_fields=["execution_username", "execution_password"])
            messages.success(request, "Database permissions saved. PostgreSQL will independently check the execution login's privileges.")
            return redirect("employee_permissions", pk=pk)
        except ValidationError as error:
            form.add_error(None, error)
    return render(request, "accounts/permissions.html", {"form": form, "employee": employee,
                  "grants": employee.database_grants.select_related("server")})


@login_required
@require_POST
def revoke(request, pk, grant_pk):
    admin = administrator(request)
    employee = get_object_or_404(Membership, user_id=pk, organization=admin.organization)
    grant = get_object_or_404(DatabaseGrant, pk=grant_pk, membership=employee)
    revoke_database(request.user, employee, grant.server, grant.database)
    messages.success(request, "Database access revoked.")
    return redirect("employee_permissions", pk=pk)


@login_required
def organization_edit(request):
    admin = administrator(request)
    organization = admin.organization
    before = {"name": organization.name, "restrict_creation": organization.restrict_creation, "allowed_ips": organization.allowed_ips}
    form = OrganizationSettingsForm(request.POST or None, instance=organization)
    if request.method == "POST" and form.is_valid():
        form.save()
        changed = {key: {"from": before[key], "to": getattr(organization, key)} for key in before if before[key] != getattr(organization, key)}
        record(request.user, "organization.edit", None, "Organization settings updated: " + (", ".join(changed) or "nothing changed"),
               payload={"organization": organization.pk, "changed": changed})
        messages.success(request, "Organization settings saved.")
        return redirect("employee_list")
    note = (f"Your address now is {network.client_ip(request) or 'unknown'}. Super Users (you included) can sign in from any address; "
            "the list applies to everyone else, on every request after they sign in.")
    return render(request, "accounts/form.html", {"form": form, "title": "Organization settings", "note": note,
                                                  "can_create_organizations": not organization.restrict_creation})


@login_required
def organization_create(request):
    """Create another organization with its first Super User, in one step. Only a Super User whose own organization allows it."""
    admin = administrator(request)
    if admin.organization.restrict_creation:
        raise PermissionDenied("Creating organizations is restricted for your organization. A Super User can change that in Organization settings.")
    form = NewOrganizationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            organization = Organization.objects.create(name=form.cleaned_data["organization"])          # restricted by default
            user = form.save(commit=False)
            user.first_name, user.last_name = form.cleaned_data["name"], ""
            user.is_active = True
            user.is_superuser = user.is_staff = True
            user.set_password(form.cleaned_data["password"])
            user.save()
            Membership.objects.create(user=user, organization=organization, role="super_user")
            record(request.user, "organization.create", None, f"Organization {organization.name!r} created with its first Super User",
                   payload={"organization": organization.pk, "super_user": user.pk})
        messages.success(request, f"Organization {organization.name} was created. Its first Super User ({user.email}) can sign in now. "
                                  "You will not see anything of that organization.")
        return redirect("organization_edit")
    return render(request, "accounts/form.html", {"form": form, "title": "Create organization",
                                                  "note": "The new organization starts with one Super User, who then adds its people and servers. "
                                                          "Nothing of it is visible to you afterwards."})