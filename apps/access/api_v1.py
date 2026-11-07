"""
Device API v1: byte-compatible with the legacy system, for today's front-door firmware (spec 05 §5.4).

Rules the door firmware depends on (tests/door_compat checks them against the real firmware):
- 2xx opens the door; the only field it reads from the body is `user.name`.
- Every refusal, check-in and unknown fob is a 4xx. A 5xx makes the door think BBMS is offline, and then any
  fob in its cache opens the door, so a 5xx must only ever mean a genuine fault.
- Requests carry the device's key in the `ApiKey` header.
"""

import json
from datetime import UTC, datetime
from functools import wraps

from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .decisions import Outcome, decide, find_fob
from .models import Device


def _error(message, status):
    return JsonResponse({"error": message}, status=status)


def device_api(view):
    """Authenticate the device by its ApiKey header; CSRF doesn't apply to device requests."""

    @csrf_exempt
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        device = Device.for_api_key(request.headers.get("ApiKey", ""))
        if device is None:
            return _error("API Key Invalid", 403)
        request.device = device
        return view(request, *args, **kwargs)

    return wrapper


def _request_data(request) -> dict:
    """Devices send JSON; the legacy system also accepted form fields, so accept both."""
    if request.content_type == "application/json":
        try:
            data = json.loads(request.body or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return request.POST.dict()


def _parse_unix_time(value):
    try:
        return datetime.fromtimestamp(int(str(value).strip()), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _user_payload(member):
    return {
        "user": {
            "id": member.pk,
            # TODO(trustees Q9): the door posts this name to a public Discord channel; it may become first name only.
            "name": member.full_name,
            "status": member.status,
            "active": member.has_space_access,
            "key_holder": member.key_holder,
            # TODO(payments app): member credit in pence, as the legacy cash_balance.
            "cash_balance": 0,
            "profile_private": member.profile_private,
        }
    }


@require_http_methods(["GET"])
@device_api
def test(request):
    return JsonResponse(["OK"], safe=False)


@require_http_methods(["POST"])
@device_api
def boot(request):
    request.device.last_boot = timezone.now()
    request.device.save(update_fields=["last_boot"])
    return JsonResponse({})


@require_http_methods(["POST"])
@device_api
def heartbeat(request):
    request.device.last_heartbeat = timezone.now()
    request.device.save(update_fields=["last_heartbeat"])
    return JsonResponse({})


@require_http_methods(["GET"])
@device_api
def status(request, tag_id):
    fob = find_fob(tag_id)
    if fob is None:
        return _error("Key fob ID not found", 404)
    return JsonResponse(_user_payload(fob.member))


@require_http_methods(["POST"])
@device_api
def activity(request):
    data = _request_data(request)
    device_id = str(data.get("device", "")).strip()
    if device_id != request.device.device_id:
        return _error("This key doesn't belong to that device", 403)

    if request.device.role == Device.Role.EQUIPMENT:
        from apps.equipment import device_api  # machine rules live in the equipment app

        status_code, body = device_api.v1_tap(request.device, str(data.get("tagId", "")), _user_payload)
        return JsonResponse(body, status=status_code)

    decision = decide(request.device, str(data.get("tagId", "")), _parse_unix_time(data.get("occurredAt")))

    if decision.outcome in (Outcome.OPEN, Outcome.CHECKED_OUT):
        return JsonResponse({"activityId": None, **_user_payload(decision.member)}, status=201)
    if decision.fob is None:
        return _error("Key fob ID not found", 404)
    # Check-ins and refusals are both 4xx, so today's door stays shut and doesn't cache the fob.
    return _error(decision.reason, 422)


@require_http_methods(["PUT", "DELETE"])
@device_api
def activity_session(request, session_id):
    """A machine's usage session: PUT = still running, DELETE = finished (05 §5.4)."""
    from apps.equipment import device_api

    if request.method == "PUT":
        status_code, body = device_api.v1_heartbeat(request.device, session_id)
    else:
        status_code, body = device_api.v1_end(request.device, session_id)
    if body is None:
        return HttpResponse(status=status_code)
    return JsonResponse(body, status=status_code)
