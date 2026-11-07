from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache


def home(request):
    return render(request, "core/home.html")


@never_cache
def healthz(request):
    """Used by App Platform and uptime monitoring: 200 if the app can reach its database."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:  # noqa: BLE001 - any database failure means unhealthy
        return JsonResponse({"status": "error", "database": "unreachable"}, status=503)
    return JsonResponse({"status": "ok"})
