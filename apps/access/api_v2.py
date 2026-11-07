"""
Device API v2, for the new firmware (spec 05 §5.4). Only the QR code endpoint exists so far.

Devices authenticate with `Authorization: Bearer <device key>`. The v1 `ApiKey` header is also accepted, so a
device can use the same key on both.
"""

from functools import wraps

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotModified, JsonResponse
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import qr
from .models import Device


def device_api_v2(view):
    @csrf_exempt
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        auth = request.headers.get("Authorization", "")
        key = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else request.headers.get("ApiKey", "")
        device = Device.for_api_key(key)
        if device is None:
            return JsonResponse({"error": "invalid_device_key"}, status=401)
        request.device = device
        return view(request, *args, **kwargs)

    return wrapper


def device_page_url(device: Device) -> str:
    """The short public link a device shows as a QR code."""
    return settings.SITE_URL + reverse("access:device-short-link", args=[device.device_id])


@require_http_methods(["GET"])
@device_api_v2
def qr_code(request):
    """The QR code for this device's page.

    Default: raw bytes for a microcontroller (format described in apps/access/qr.py).
    `?format=json`: the same data as JSON, for debugging.
    Send `If-None-Match` with the last ETag to get 304 Not Modified when nothing has changed.
    """
    url = device_page_url(request.device)
    tag = qr.etag(url)
    if request.headers.get("If-None-Match") == tag:
        response = HttpResponseNotModified()
        response["ETag"] = tag
        return response

    if request.GET.get("format") == "json":
        size, rows = qr.packed_rows(url)
        response = JsonResponse(
            {"url": url, "size": size, "row_bytes": len(rows[0]), "rows": [row.hex() for row in rows]}
        )
    else:
        response = HttpResponse(qr.raw(url), content_type="application/octet-stream")
    response["ETag"] = tag
    response["X-QR-URL"] = url
    return response
