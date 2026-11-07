"""Pages about devices, reached from the QR code on each device's screen."""

from django.contrib.auth.decorators import login_required
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
