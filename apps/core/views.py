from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache


def home(request):
    """The home page. Visitors see how many people are in the space, if the trustees turned that on (06 §6.7)."""
    from apps.access import activity
    from apps.site_settings.models import SiteSettings

    in_space = None
    if not request.user.is_authenticated and SiteSettings.load().public_occupancy_count:
        in_space = activity.count()
    return render(request, "core/home.html", {"in_space": in_space})


@never_cache
def healthz(request):
    """Used by App Platform and uptime monitoring: 200 if the app can reach its database."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:  # noqa: BLE001 - any database failure means unhealthy
        return JsonResponse({"status": "error", "database": "unreachable"}, status=503)
    return JsonResponse({"status": "ok"})
