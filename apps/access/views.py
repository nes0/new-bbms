"""Pages about devices, reached from the QR code on each device's screen, and the fake device for local testing."""

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from .models import Device


def device_short_link(request, device_id):
    """The short address in the QR code (kept short so the code stays small)."""
    get_object_or_404(Device, device_id=device_id)
    return redirect("access:device-page", device_id=device_id)


@login_required
def device_page(request, device_id):
    # TODO(equipment app): for equipment devices this becomes the machine's page (rate, status, training, use).
    device = get_object_or_404(Device, device_id=device_id)
    return render(request, "access/device.html", {"device": device})


def fake_device(request):
    """A pretend machine fob reader that uses the v2 device API, for trying it out locally.

    Only when DEBUG is on: it is a development tool, not part of the live site. The page itself holds no secrets;
    you paste a device key into it (see `manage.py create_demo_device`).
    """
    if not settings.DEBUG:
        raise Http404
    return render(request, "access/fake_device.html")
