"""Device API (v1 at the legacy addresses, v2 for new firmware) and device pages (spec 05 §5.4)."""

from django.urls import path

from apps.equipment import api_v2 as machine_api

from . import activity_views, api_v1, api_v2, views

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
    path("api/devices/v2/check-in", api_v2.check_in, name="v2-check-in"),
    path("api/devices/v2/access", api_v2.access, name="v2-access"),
    path("api/devices/v2/allowed-fobs", api_v2.allowed_fobs, name="v2-allowed-fobs"),
    path("api/devices/v2/offline-events", api_v2.offline_events, name="v2-offline-events"),
    path("api/devices/v2/sessions/<int:session_id>/heartbeat", machine_api.session_heartbeat, name="v2-heartbeat"),
    path("api/devices/v2/sessions/<int:session_id>/end", machine_api.session_end, name="v2-end"),
    path("api/devices/v2/machine-status", machine_api.machine_status, name="v2-machine-status"),
    path("api/devices/v2/meter", machine_api.meter, name="v2-meter"),
    # Activity and who's in the space (06 §6.7)
    path("activity/", activity_views.activity_page, name="activity"),
    path("activity/in-now/", activity_views.in_now, name="in-now"),
    path("occupancy.json", activity_views.occupancy, name="occupancy"),
    # Device pages, linked from the QR code on each device's screen
    path("d/<slug:device_id>", views.device_short_link, name="device-short-link"),
    path("devices/<slug:device_id>/", views.device_page, name="device-page"),
    # Local development only (404 unless DEBUG): a pretend machine reader using the v2 API
    path("dev/fake-device/", views.fake_device, name="fake-device"),
]
