"""Device API (v1 at the legacy addresses, v2 for new firmware) and device pages (spec 05 §5.4)."""

from django.urls import path

from . import api_v1, api_v2, views

app_name = "access"

urlpatterns = [
    path("acs/test", api_v1.test, name="v1-test"),
    path("acs/node/boot", api_v1.boot, name="v1-boot"),
    path("acs/node/heartbeat", api_v1.heartbeat, name="v1-heartbeat"),
    path("acs/status/<str:tag_id>", api_v1.status, name="v1-status"),
    path("acs/activity", api_v1.activity, name="v1-activity"),
    path("acs/activity/<int:session_id>", api_v1.activity_session, name="v1-activity-session"),
    # Device API v2 (new firmware)
    path("api/devices/v2/qr", api_v2.qr_code, name="v2-qr"),
    # Device pages, linked from the QR code on each device's screen
    path("d/<slug:device_id>", views.device_short_link, name="device-short-link"),
    path("devices/<slug:device_id>/", views.device_page, name="device-page"),
]
