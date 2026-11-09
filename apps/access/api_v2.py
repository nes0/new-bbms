"""
Device API v2, for the new firmware (spec 05 §5.4, build brief in spec 09).

Devices authenticate with `Authorization: Bearer <device key>`. The v1 `ApiKey` header is also accepted, so a
device can use the same key on both.

The HTTP status describes the transport and the body describes the decision: every decision, allowed or refused,
is a 200. The views for machines (sessions, meter, machine status) are in apps/equipment/api_v2.py.
"""

import hashlib
import json
from functools import wraps

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse, HttpResponseNotModified, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.site_settings.models import SiteSettings

from . import activity, decisions, qr, screen, tags
from .models import AccessEvent, Device, KeyFob

# The check-in and session heartbeat intervals are admin settings (apps/site_settings, 02 §2.8).
MAX_OFFLINE_EVENTS = 500


class ApiError(Exception):
    """Turned into a JSON error response by the device_api_v2 decorator."""

    def __init__(self, status, error, detail=""):
        self.status, self.error, self.detail = status, error, detail


def unix(moment) -> int:
    return int(moment.timestamp())


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
        # Any request proves the device is alive.
        device.last_heartbeat = timezone.now()
        Device.objects.filter(pk=device.pk).update(last_heartbeat=device.last_heartbeat)
        try:
            return view(request, *args, **kwargs)
        except ApiError as error:
            body = {"error": error.error}
            if error.detail:
                body["detail"] = error.detail
            return JsonResponse(body, status=error.status)

    return wrapper


def json_body(request, *, required=True) -> dict:
    """The request's JSON object. An empty body counts as {} unless `required`."""
    if not request.body:
        if required:
            raise ApiError(400, "bad_json", "A JSON body is needed.")
        return {}
    try:
        data = json.loads(request.body)
    except ValueError:
        raise ApiError(400, "bad_json", "The body isn't valid JSON.") from None
    if not isinstance(data, dict):
        raise ApiError(400, "bad_json", "The body must be a JSON object.")
    return data


def int_field(data, name, *, minimum=None, maximum=None, required=True):
    value = data.get(name)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, "bad_field", f"{name} must be a whole number.")
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ApiError(400, "bad_field", f"{name} is out of range.")
    return value


def resolve_card(data):
    """(fob, reason) for the request's `card`; 400 for a missing or malformed one."""
    card = data.get("card")
    if not isinstance(card, str | int) or isinstance(card, bool) or card == "":
        raise ApiError(400, "bad_card", "card is needed.")
    try:
        return decisions.find_fob_by_card(str(card))
    except ValueError:
        raise ApiError(400, "bad_card", "card must be 1-10 digits or a 12-character tag.") from None


def enrolled(device, data, reason):
    """An unknown fob tapped while an enrolment window is open on this reader: capture it (05 §5.1)."""
    if reason != "unknown_fob":
        return None
    from .fobs import capture_v2_card

    return capture_v2_card(device, data.get("card"))


def equipment_or_error(device):
    """The machine this reader belongs to, or a 409 (spec 09 §9.3)."""
    from apps.equipment.device_api import equipment_for_device

    if device.role != Device.Role.EQUIPMENT:
        raise ApiError(409, "wrong_device_role")
    equipment = equipment_for_device(device)
    if equipment is None:
        raise ApiError(409, "no_equipment")
    return equipment


# --- the fobs a device may accept offline, and the version that tells it when they change (9.7) ---


def allowed_fob_list(device) -> list[dict]:
    if device.role == Device.Role.EQUIPMENT:
        from apps.equipment import rules

        equipment = equipment_or_error(device)

        def allowed(member):
            return not rules.machine_refusal_reason(member, equipment, for_offline_cache=True)
    else:

        def allowed(member):
            return not decisions.door_refusal_reason(member)

    fobs = KeyFob.objects.filter(active=True).select_related("member")
    entries = []
    for fob in fobs:
        if allowed(fob.member):
            card = tags.format_card_number(fob.card_number) if fob.card_number is not None else None
            entries.append(
                {"card": card, "tag": fob.tag_id, "display_name": fob.member.display_name, "_sort": fob.card_number}
            )
    entries.sort(key=lambda e: (e["_sort"] is None, e["_sort"] or 0, e["tag"] or ""))
    for entry in entries:
        del entry["_sort"]
    return entries


def cache_version(entries) -> str:
    """First 8 hex characters of SHA-256 over the sorted cards and names. Computed on demand, never stored."""
    text = "\n".join(f"{e['card']}|{e['tag']}|{e['display_name']}" for e in entries)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


@require_http_methods(["POST"])
@device_api_v2
def check_in(request):
    """Boot or heartbeat: refreshes the device record and hands back timings, commands and (machines) config."""
    device = request.device
    data = json_body(request)
    event = data.get("event")
    if event not in ("boot", "heartbeat"):
        raise ApiError(400, "bad_event", 'event must be "boot" or "heartbeat".')
    firmware = data.get("firmware_version") or ""
    reported = data.get("cache_version") or ""
    if not isinstance(firmware, str) or not isinstance(reported, str):
        raise ApiError(400, "bad_field", "firmware_version and cache_version must be strings.")

    now = timezone.now()
    if event == "boot":
        device.last_boot = now
    device.last_heartbeat = now
    device.firmware_version = firmware[:50]
    device.reported_cache_version = reported[:20]

    version = cache_version(allowed_fob_list(device)) if _can_list(device) else ""
    commands = list(device.pending_commands)
    if not device.enabled:
        commands.append("disable")
    if version and reported != version and "refresh_cache" not in commands:
        commands.append("refresh_cache")
    device.pending_commands = []
    device.save(
        update_fields=["last_boot", "last_heartbeat", "firmware_version", "reported_cache_version", "pending_commands"]
    )

    site_settings = SiteSettings.load()
    body = {
        "server_time": unix(now),
        "heartbeat_seconds": site_settings.reader_check_in_seconds,
        "session_heartbeat_seconds": site_settings.session_heartbeat_seconds,
        "cache_version": version,
        "commands": commands,
    }
    if device.role == Device.Role.EQUIPMENT:
        from apps.equipment.api_v2 import machine_payload
        from apps.equipment.device_api import equipment_for_device

        equipment = equipment_for_device(device)
        if equipment is not None:
            body["machine"] = machine_payload(equipment)
    return JsonResponse(body)


def _can_list(device) -> bool:
    """A machine reader whose equipment is missing has no list to offer."""
    if device.role != Device.Role.EQUIPMENT:
        return True
    from apps.equipment.device_api import equipment_for_device

    return equipment_for_device(device) is not None


@require_http_methods(["POST"])
@device_api_v2
def access(request):
    """A fob tap. Entry and exit readers are decided here; machines in apps/equipment/api_v2.py."""
    device = request.device
    data = json_body(request)
    fob, reason = resolve_card(data)

    if device.role == Device.Role.EQUIPMENT:
        from apps.equipment.api_v2 import equipment_access

        enrolment = enrolled(device, data, reason) if fob is None and device.enabled else None
        return JsonResponse(equipment_access(device, fob, reason, enabled=device.enabled, enrolment=enrolment))

    if not device.enabled:
        return _door_reply(False, "device_disabled", None)
    if fob is None:
        enrolment = enrolled(device, data, reason)
        if enrolment:
            return _door_reply(False, "enrolled", enrolment.member.display_name)
        return _door_reply(False, reason, None)

    decision = decisions.decide(device, "", timezone.now(), fob=fob)
    reasons = {
        decisions.Outcome.OPEN: ("ok", True),
        decisions.Outcome.CHECKED_IN: ("checked_in", False),  # visit recorded; the door stays shut
        decisions.Outcome.CHECKED_OUT: ("checked_out", True),
    }
    code, allowed = reasons.get(decision.outcome, (decision.reason, False))
    return _door_reply(allowed, code, decision.member.display_name)


def _door_reply(allowed, reason, display_name):
    """A door or exit reader's reply, with how many are in the space now (after this tap) for its screen."""
    return JsonResponse(
        {"allowed": allowed, "reason": reason, "display_name": display_name, "in_space": activity.count()}
    )


@require_http_methods(["GET"])
@device_api_v2
def allowed_fobs(request):
    entries = allowed_fob_list(request.device)
    version = cache_version(entries)
    etag = f'"{version}"'
    if request.headers.get("If-None-Match") == etag:
        response = HttpResponseNotModified()
    else:
        response = JsonResponse({"cache_version": version, "fobs": entries})
    response["ETag"] = etag
    return response


# --- offline events (9.7) ---


@require_http_methods(["POST"])
@device_api_v2
def offline_events(request):
    """Events a device stored while BBMS was unreachable. Resending is safe: each id is recorded once."""
    from apps.equipment.models import OfflineEvent

    data = json_body(request)
    events = data.get("events")
    if not isinstance(events, list):
        raise ApiError(400, "bad_field", "events must be a list.")
    if len(events) > MAX_OFFLINE_EVENTS:
        raise ApiError(400, "too_many_events", f"At most {MAX_OFFLINE_EVENTS} events per call.")

    results = []
    for event in events:
        event_id = event.get("id") if isinstance(event, dict) else None
        if not isinstance(event_id, str) or not event_id or len(event_id) > 64:
            results.append({"id": event_id if isinstance(event_id, str) else None, "result": "invalid"})
            continue
        with transaction.atomic():
            if OfflineEvent.objects.filter(device=request.device, event_id=event_id).exists():
                results.append({"id": event_id, "result": "duplicate"})
                continue
            result = _record_offline_event(request.device, event)
            OfflineEvent.objects.create(
                device=request.device,
                event_id=event_id,
                type=str(event.get("type", ""))[:20],
                payload=event,
                result=result,
            )
        results.append({"id": event_id, "result": result})
    return JsonResponse({"results": results})


def event_time(event, name):
    value = event.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    try:
        from datetime import UTC, datetime

        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _record_offline_event(device, event) -> str:
    try:
        fob, reason = decisions.find_fob_by_card(str(event.get("card", "")))
    except ValueError:
        return "invalid"
    kind = event.get("type")
    if kind == "door" and device.role != Device.Role.EQUIPMENT:
        occurred_at = event_time(event, "occurred_at")
        if occurred_at is None or not isinstance(event.get("allowed"), bool):
            return "invalid"
        if fob is None:
            return "unknown_fob"
        now = timezone.now()
        allowed = event["allowed"]
        # Record what happened; don't re-judge it.
        AccessEvent.objects.create(
            kind=AccessEvent.Kind.ENTRY if allowed else AccessEvent.Kind.REFUSED,
            member=fob.member,
            fob=fob,
            device=device,
            reason="" if allowed else "offline",
            occurred_at=min(occurred_at, now),
            received_at=now,
            delayed=True,
        )
        return "recorded"
    if kind == "session" and device.role == Device.Role.EQUIPMENT:
        from apps.equipment.api_v2 import record_offline_session

        return record_offline_session(device, fob, event)
    return "invalid"


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


# --- the occupancy screen (05 §5.4, 06 §6.7) ---


def door_side_or_error(device):
    """The occupancy screen is for readers by the door (entry and exit), not machines."""
    if device.role == Device.Role.EQUIPMENT:
        raise ApiError(409, "wrong_device_role", "Only entry and exit readers show who's in.")


@require_http_methods(["GET"])
@device_api_v2
def occupancy(request):
    """Who's in the space now, for a screen by the door. Send `If-None-Match` with the last ETag to get 304."""
    door_side_or_error(request.device)
    now = timezone.now()
    entries = screen.people(now)
    version = screen.version(entries)
    etag = f'"{version}"'
    if request.headers.get("If-None-Match") == etag:
        response = HttpResponseNotModified()
    else:
        response = JsonResponse({"in_space": len(entries), "as_of": unix(now), "version": version, "people": entries})
    response["ETag"] = etag
    return response


@require_http_methods(["GET"])
@device_api_v2
def member_photo(request, member_id):
    """A member's photo for the occupancy screen: only while they're in the space, and only if it may be shown.

    `?size=` 16-200 pixels square (64 by default); `?format=` rgb565 (default), png or jpeg.
    """
    door_side_or_error(request.device)
    fmt = request.GET.get("format", "rgb565")
    if fmt not in screen.FORMATS:
        raise ApiError(400, "bad_field", "format must be rgb565, png or jpeg.")
    try:
        size = int(request.GET.get("size", screen.DEFAULT_SIZE))
    except ValueError:
        raise ApiError(400, "bad_field", "size must be a whole number of pixels.") from None
    if not screen.MIN_SIZE <= size <= screen.MAX_SIZE:
        raise ApiError(400, "bad_field", f"size must be from {screen.MIN_SIZE} to {screen.MAX_SIZE}.")
    from apps.members.models import Member

    member = Member.objects.filter(pk=member_id).select_related("profile").first()
    photo = screen.shown_photo(member) if member is not None and screen.is_in(member) else None
    if photo is None:
        raise ApiError(404, "no_photo", "No photo to show for that member now.")
    etag = f'"{screen.photo_version(photo)}-{size}-{fmt}"'
    if request.headers.get("If-None-Match") == etag:
        response = HttpResponseNotModified()
    else:
        response = HttpResponse(screen.render(photo, size, fmt), content_type=screen.FORMATS[fmt])
    response["ETag"] = etag
    response["Cache-Control"] = "private, no-store"
    return response
