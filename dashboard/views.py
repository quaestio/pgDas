from django.contrib.auth.decorators import login_required
from django.http import JsonResponse

from . import data


@login_required
def dashboard_data(request):
    """The numbers behind the home page (the Servers node of the Object Browser)."""
    return JsonResponse({"ok": True, **data.build(request.user)})
