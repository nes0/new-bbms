"""
Watching the devices (spec 05 §5.5).

A device that's marked "monitor heartbeat" and hasn't been heard from for over an hour is reported once, to the
`acs` role and the admins, as a notification. The key includes the device's last heartbeat, so one outage is reported
once however often this runs, and a device that comes back and goes quiet again is reported again.
"""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from apps.notifications.notify import notify_role

from .models import Device

ACS_ROLE = "acs"
QUIET_FOR = timedelta(hours=1)


def check_devices(now=None) -> int:
    """Report devices that have gone quiet. Returns how many people were told."""
    now = now or timezone.now()
    told = 0
    quiet = Device.objects.filter(
        enabled=True, monitor_heartbeat=True, last_heartbeat__isnull=False, last_heartbeat__lt=now - QUIET_FOR
    )
    for device in quiet:
        last = timezone.localtime(device.last_heartbeat)
        told += notify_role(
            ACS_ROLE,
            admins=True,
            message=f"{device.name} hasn't been heard from since {last:%-d %b %H:%M}.",
            kind="device_offline",
            key=f"device-offline:{device.pk}:{int(device.last_heartbeat.timestamp())}",
            url=reverse("admin:access_device_change", args=[device.pk]),
        )
    return told
