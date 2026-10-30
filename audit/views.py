from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.shortcuts import render

from .models import AuditEntry


@login_required
@user_passes_test(lambda user: user.is_staff)
def audit_list(request):
    page = Paginator(AuditEntry.objects.all(), 50).get_page(request.GET.get("page"))
    return render(request, "audit/list.html", {"page": page})
