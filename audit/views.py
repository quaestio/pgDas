import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from explorer import writes
from explorer.pg import PgError

from .models import AuditEntry, record

staff_required = user_passes_test(lambda user: user.is_staff)


@login_required
@staff_required
def audit_list(request):
    page = Paginator(AuditEntry.objects.all(), 50).get_page(request.GET.get("page"))
    return render(request, "audit/list.html", {"page": page, "confirm_word": writes.CONFIRM_WORD})


@login_required
@staff_required
def audit_json(request, pk):
    """The whole JSON record of a change (every deleted row), as a download."""
    entry = get_object_or_404(AuditEntry, pk=pk)
    if not entry.payload:
        return HttpResponse("This audit entry has no JSON record.", status=404, content_type="text/plain")
    response = HttpResponse(json.dumps(entry.payload, indent=2, default=str, ensure_ascii=False),
                            content_type="application/json; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="pgdeck-audit-{entry.pk}.json"'
    return response


@login_required
@staff_required
@require_POST
def audit_reinsert(request, pk):
    """Put the rows of a recorded delete back. Asked for through a confirmation popup; works once per record."""
    entry = get_object_or_404(AuditEntry, pk=pk)
    try:
        result = writes.reinsert(entry, request.user)
    except PgError as error:
        record(request.user, "delete.restore", entry.server, f"Re-insert from audit entry #{entry.pk} failed: {error}", ok=False)
        messages.error(request, f"Could not re-insert: {error}")
    else:
        messages.success(request, f"Re-inserted {result['count']} row{'s' if result['count'] != 1 else ''} "
                                  f"into {result['schema']}.{result['table']}.")
    page = request.POST.get("page", "")
    return redirect(f"{reverse('audit_list')}?page={page}" if page.isdigit() else "audit_list")
