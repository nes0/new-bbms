"""
What machine readers' requests do (spec 05 §5.3, §5.4). The HTTP layer is in apps/access/api_v1.py.

Each function returns (http_status, body) so the v1 views stay thin. v1 matches the legacy system:
- a tap starts a session (closing any other open session on that machine) -> 201 {activityId, user};
- PUT  /acs/activity/<id> -> heartbeat, 200;  DELETE -> end, 204;  a finished session -> 400;
- refusals -> 422, unknown fob -> 404.
"""

from apps.access.decisions import find_fob

from . import rules
from .models import Equipment, UsageSession


def equipment_for_device(device):
    # Legacy convention: a machine reader's device id is the equipment's slug (05 §5.3).
    return Equipment.objects.filter(slug=device.device_id, archived=False).first()


def v1_tap(device, raw_tag, user_payload):
    equipment = equipment_for_device(device)
    if equipment is None:
        return 422, {"error": "Invalid Device Key"}
    fob = find_fob(raw_tag)
    if fob is None:
        from apps.access.fobs import capture_v1_tag

        capture_v1_tag(device, raw_tag)  # during an enrolment window (05 §5.1); still refused
        return 404, {"error": "Key fob ID not found"}
    reason = rules.machine_refusal_reason(fob.member, equipment)
    if reason:
        return 422, {"error": reason}
    session = rules.start_session(equipment, fob.member, fob, close_existing=True)
    return 201, {"activityId": session.pk, **user_payload(fob.member)}


def _session_for(device, session_id):
    equipment = equipment_for_device(device)
    if equipment is None:
        return None
    return UsageSession.objects.filter(pk=session_id, equipment=equipment).first()


def v1_heartbeat(device, session_id):
    session = _session_for(device, session_id)
    if session is None:
        return 404, {"error": "Session not found"}
    if not session.is_open:
        return 400, {"error": "Session already finished"}
    rules.heartbeat(session)
    return 200, {}


def v1_end(device, session_id):
    session = _session_for(device, session_id)
    if session is None:
        return 404, {"error": "Session not found"}
    if not session.is_open:
        return 400, {"error": "Session already finished"}
    rules.end_session(session, ended_by=UsageSession.EndedBy.DEVICE)
    return 204, None
