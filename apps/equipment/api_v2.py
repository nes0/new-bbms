"""
Device API v2, the machine-only parts (spec 09 §9.5-9.7). Door readers and shared plumbing: apps/access/api_v2.py.

Every decision is a 200 with a body; see spec 09 §9.3. Ending, metering and offline events are idempotent,
because firmware retries after timeouts.
"""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.access.api_v2 import ApiError, device_api_v2, equipment_or_error, int_field, json_body, resolve_card, unix

from . import rules
from .models import Equipment, MachineStatusChange, MeterReading, UsageSession

RECENT_SESSION = timedelta(hours=1)  # a meter reading may still be credited to a session that ended this recently


def machine_payload(equipment: Equipment) -> dict:
    """What a reader shows and uses about its machine. Sent by check-in, access, end and machine-status."""
    session = rules.open_session(equipment)
    current_mode = equipment.meter_mode == Equipment.MeterMode.CURRENT
    threshold = equipment.current_threshold_amps
    return {
        "name": equipment.name,
        "config_version": equipment.config_version,
        "hourly_rate_pence": equipment.usage_cost_pence_per_hour,
        "minimum_minutes": rules.minimum_charge_seconds() // 60,
        "status": equipment.status,
        "status_note": equipment.status_note,
        "status_since": unix(equipment.status_since),
        "in_use_by": session.member.display_name if session else None,
        "session_id": session.pk if session else None,
        "meter": {
            "mode": equipment.meter_mode,
            "current_threshold_amps": str(threshold) if current_mode and threshold is not None else None,
        },
    }


def _locked_equipment(equipment: Equipment) -> Equipment:
    # Two taps arriving together mustn't both start a session.
    return Equipment.objects.select_for_update().get(pk=equipment.pk)


def equipment_access(device, fob, reason, *, enabled=True, enrolment=None) -> dict:
    """A tap at a machine reader (spec 09 §9.5)."""
    equipment = equipment_or_error(device)
    if not enabled:
        return {
            "allowed": False,
            "reason": "device_disabled",
            "display_name": None,
            "machine": machine_payload(equipment),
        }
    if fob is None:
        if enrolment is not None:  # captured for a member during an enrolment window (spec 05 §5.1)
            name = enrolment.member.display_name
            return {"allowed": False, "reason": "enrolled", "display_name": name, "machine": machine_payload(equipment)}
        return {"allowed": False, "reason": reason, "display_name": None, "machine": machine_payload(equipment)}

    member = fob.member
    with transaction.atomic():
        equipment = _locked_equipment(equipment)
        current = rules.open_session(equipment)
        body = {"allowed": False, "reason": "", "display_name": member.display_name}
        resumed = False
        if current is not None and current.member_id == member.pk:
            session, resumed = current, True  # the reader lost track, e.g. after a reboot
        elif current is not None:
            body.update(reason="in_use", in_use_by=current.member.display_name)
            session = None
        else:
            refusal = rules.machine_refusal_reason(member, equipment)
            if refusal:
                body["reason"] = refusal
                session = None
            else:
                session = rules.start_session(equipment, member, fob, close_existing=False)

        if session is not None:
            body.update(
                allowed=True,
                reason="ok",
                session_id=session.pk,
                resumed=resumed,
                session_reason=session.reason,
                balance_pence=rules.live_balance_pence(member, session),
                hourly_rate_pence=equipment.usage_cost_pence_per_hour,
            )
    body["machine"] = machine_payload(equipment)
    return body


def _own_session(device, session_id) -> tuple[Equipment, UsageSession]:
    equipment = equipment_or_error(device)
    session = (
        UsageSession.objects.filter(pk=session_id, equipment=equipment).select_related("member", "equipment").first()
    )
    if session is None:
        raise ApiError(404, "session_not_found")
    return equipment, session


def _end_summary(session: UsageSession, equipment: Equipment, *, already_ended: bool) -> dict:
    return {
        "ended": True,
        "already_ended": already_ended,
        "duration_seconds": session.duration_seconds,
        "fee_pence": session.fee_pence or 0,
        "removed": session.removed,
        "session_reason": session.reason,
        "balance_pence": rules.live_balance_pence(session.member, session),
        "machine": machine_payload(equipment),
    }


@require_http_methods(["POST"])
@device_api_v2
def session_heartbeat(request, session_id):
    equipment, session = _own_session(request.device, session_id)
    json_body(request, required=False)
    if not session.is_open:
        # A fact, not an error: the device goes back to idle (v1 said 400 here).
        summary = _end_summary(session, equipment, already_ended=True)
        return JsonResponse({**summary, "open": False, "ended_by": session.ended_by})
    rules.heartbeat(session)
    fee = rules.fee_so_far_pence(session)
    return JsonResponse(
        {
            "open": True,
            "elapsed_seconds": session.duration_seconds,
            "fee_so_far_pence": fee,
            "balance_pence": rules.live_balance_pence(session.member, session),
        }
    )


@require_http_methods(["POST"])
@device_api_v2
def session_end(request, session_id):
    data = json_body(request)
    equipment, session = _own_session(request.device, session_id)
    has_card = data.get("card") not in (None, "")
    if not has_card and data.get("ended_by") != "button":
        raise ApiError(400, "bad_request", 'Send "card" for a tap-out or {"ended_by": "button"}.')
    fob = reason = None
    if has_card:
        fob, reason = resolve_card(data)

    with transaction.atomic():
        equipment = _locked_equipment(equipment)
        session = UsageSession.objects.select_for_update().select_related("member", "equipment").get(pk=session.pk)
        if not session.is_open:
            return JsonResponse(_end_summary(session, equipment, already_ended=True))  # idempotent
        if has_card:
            if fob is None:
                return JsonResponse({"ended": False, "reason": reason})
            if fob.member_id != session.member_id:
                return JsonResponse({"ended": False, "reason": "in_use", "in_use_by": session.member.display_name})
            ended_by = UsageSession.EndedBy.TAP
        else:
            ended_by = UsageSession.EndedBy.BUTTON
        rules.end_session(session, ended_by=ended_by)
    return JsonResponse(_end_summary(session, equipment, already_ended=False))


@require_http_methods(["POST"])
@device_api_v2
def machine_status(request):
    """The out-of-order button (spec 09 §9.6). Only a tapped-in member can press it."""
    equipment = equipment_or_error(request.device)
    data = json_body(request)
    status = data.get("status")
    if status not in Equipment.Status.values:
        raise ApiError(400, "bad_status", 'status must be "working" or "out_of_order".')
    note = data.get("note") or ""
    if not isinstance(note, str):
        raise ApiError(400, "bad_field", "note must be text.")
    session_id = int_field(data, "session_id", required=False)

    with transaction.atomic():
        equipment = _locked_equipment(equipment)
        session = None
        if session_id is not None:
            session = (
                UsageSession.objects.filter(pk=session_id, equipment=equipment, ended_at__isnull=True)
                .select_related("member")
                .first()
            )
        if session is None:
            return JsonResponse({"changed": False, "reason": "not_tapped_in"})
        if equipment.status == status:
            return JsonResponse({"changed": False, "reason": "no_change"})
        equipment.status = status
        equipment.status_note = note[:255]
        equipment.status_since = timezone.now()
        equipment.save(update_fields=["status", "status_note", "status_since"])
        MachineStatusChange.objects.create(
            equipment=equipment,
            status=status,
            note=equipment.status_note,
            reported_by=session.member,
            session=session,
            source=MachineStatusChange.Source.BUTTON,
        )
        label = "out of order" if status == Equipment.Status.OUT_OF_ORDER else "working again"
        missing_reason = status == Equipment.Status.OUT_OF_ORDER and not equipment.status_note
        reason = " No reason given yet." if missing_reason else f" {equipment.status_note}"
        who = session.member.display_name
        rules.notify_team(equipment, f"{who} reported {equipment.name} {label}.{reason}".rstrip())
        if missing_reason:
            # A reader has no keyboard: ask whoever pressed the button to say what's wrong on the machine's page.
            rules.ask_reporter_for_reason(equipment, session.member)
    return JsonResponse({"changed": True, "machine": machine_payload(equipment)})


@require_http_methods(["POST"])
@device_api_v2
def meter(request):
    """Hour-meter reading: seconds the motor ran. Exact repeats of (device, seq) are ignored."""
    equipment = equipment_or_error(request.device)
    data = json_body(request)
    seq = int_field(data, "seq", minimum=0)
    active_seconds = int_field(data, "active_seconds", minimum=0, maximum=86400)
    session_id = int_field(data, "session_id", required=False)

    if equipment.meter_mode != Equipment.MeterMode.CURRENT:
        return JsonResponse({"accepted": False, "reason": "session_metered"})

    now = timezone.now()
    session = None
    if session_id is not None:
        session = UsageSession.objects.filter(pk=session_id, equipment=equipment).first()
        if session is not None and session.ended_at and now - session.ended_at > RECENT_SESSION:
            session = None
    try:
        with transaction.atomic():
            MeterReading.objects.create(
                device=request.device, equipment=equipment, session=session, seq=seq, active_seconds=active_seconds
            )
            if session is not None and active_seconds:
                UsageSession.objects.filter(pk=session.pk).update(
                    motor_on_seconds=F("motor_on_seconds") + active_seconds
                )
    except IntegrityError:
        return JsonResponse({"accepted": False, "reason": "duplicate"})
    rules.auto_end_idle_sessions(now)
    return JsonResponse({"accepted": True})


def record_offline_session(device, fob, event) -> str:
    """A finished session the reader ran while BBMS was unreachable (spec 09 §9.7). Returns the result code."""
    from apps.access.api_v2 import event_time

    equipment = equipment_or_error(device)
    started, ended = event_time(event, "started_at"), event_time(event, "ended_at")
    motor = event.get("motor_on_seconds", 0)
    if started is None or ended is None or ended <= started or isinstance(motor, bool) or not isinstance(motor, int):
        return "invalid"
    if fob is None:
        return "unknown_fob"
    ended_by = event.get("ended_by")
    if ended_by not in UsageSession.EndedBy.values:
        ended_by = UsageSession.EndedBy.DEVICE

    with transaction.atomic():
        equipment = _locked_equipment(equipment)
        overlaps = (
            UsageSession.objects.filter(equipment=equipment, started_at__lt=ended)
            .exclude(ended_at__lte=started)
            .exists()
        )
        if overlaps:
            return "overlap"
        session = UsageSession.objects.create(
            equipment=equipment,
            member=fob.member,
            fob=fob,
            started_at=started,
            last_heartbeat_at=ended,
            motor_on_seconds=max(0, motor),
        )
        rules.end_session(session, when=ended, ended_by=ended_by)
    return "recorded"
