from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render

from . import data


@login_required
def dashboard_page(request):
    return render(request, "dashboard/page.html", {"periods": data.TREND_DAYS})


@login_required
def dashboard_data(request):
    try:
        days = int(request.GET.get("days") or 7)
    except ValueError:
        days = 7
    return JsonResponse({"ok": True, **data.build(request.user, days)})
