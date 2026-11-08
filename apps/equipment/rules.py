"""
Who may use a machine, usage sessions and their fees (spec 05 §5.3, §5.6).

Plain functions, so the v1 API, the v2 API, the admin and the scheduled tidy-up all share one set of rules.
"""

import logging
from datetime import datetime, time, timedelta

from django.db import transaction
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from apps.notifications.notify import notify_role
from apps.payments import credit, services
from apps.site_settings.models import SiteSettings

from .models import Equipment, Induction, MeterReading, TrainingCompletion, UsageSession

logger = logging.getLogger(__name__)

# The minimum charge and the credit allowed below zero are admin settings (apps/site_settings, 02 §2.8).
# These are fixed rules of how sessions work, not prices, so they stay in the code:
SHORT_SESSION = timedelta(seconds=60)  # this short or less: removed, not charged
MERGE_GAP = timedelta(seconds=60)  # back-to-back sessions this close are merged
MERGE_WINDOW = timedelta(days=2)  # the tidy-up only looks this far back for sessions to merge
STALE_AFTER = timedelta(hours=1)  # an open session with no heartbeat for this long is closed

UNCHARGED_REASONS = (UsageSession.Reason.TRAINING, UsageSession.Reason.TESTING, UsageSession.Reason.MAINTENANCE)


def minimum_charge_seconds() -> int:
    """A charged session costs at least this much time (setting, 02 §2.8; 15 minutes to start with)."""
    return SiteSettings.load().minimum_session_minutes * 60


def member_credit_pence(member) -> int:
    """The member's credit, worked out from the ledger (04 §4.7)."""
    return credit.credit_pence(member)


def notify_team(equipment: Equipment, message: str):
    """Tell everyone on the machine's team (its managing role) in their notifications, and log it. `message` is a
    whole sentence that says which machine it's about. If the machine has no team, only the admins are told."""
    logger.info("Notify team of %s: %s", equipment.slug, message)
    roles = [equipment.team.name] if equipment.team_id else []
    notify_role(
        *roles,
        admins=not roles,
        message=message,
        kind="equipment_team",
        url=reverse("admin:equipment_equipment_change", args=[equipment.pk]),
    )


def is_on_team(member, equipment: Equipment) -> bool:
    """The machine's team (its managing role), or an admin."""
    if member.is_superuser:
        return True
    return equipment.team_id is not None and member.groups.filter(pk=equipment.team_id).exists()


def _course_reason(member, equipment: Equipment, induction: Induction) -> str:
    if not equipment.moodle_course_id:
        return ""
    if TrainingCompletion.objects.filter(member=member, moodle_course_id=equipment.moodle_course_id).exists():
        return ""
    trained_before_course = (
        equipment.moodle_course_since is not None
        and induction.trained_at is not None
        and induction.trained_at < equipment.moodle_course_since
    )
    if trained_before_course and not equipment.course_required_for_existing:
        return ""  # signed off before the course existed: keeps access (06 §6.12)
    return "course_incomplete"


def machine_refusal_reason(member, equipment: Equipment, *, for_offline_cache=False) -> str:
    """Why this member can't start a session on this machine, or "" if they can (05 §5.3).

    for_offline_cache=True ignores credit and machine status: who a reader may remember (spec 09 §9.7).
    """
    if not member.has_space_access:
        return "not_member"
    on_team = is_on_team(member, equipment)
    if equipment.status == Equipment.Status.OUT_OF_ORDER and not on_team and not for_offline_cache:
        return "out_of_order"
    if equipment.requires_induction:
        induction = Induction.objects.filter(member=member, category=equipment.induction_key).first()
        if induction is None or not induction.is_trained:
            return "not_trained"
        reason = _course_reason(member, equipment, induction)
        if reason:
            return reason
    if for_offline_cache:
        return ""
    credit_limit = SiteSettings.load().machine_credit_limit_pence
    if equipment.usage_cost_pence_per_hour and member_credit_pence(member) + credit_limit <= 0:
        return "insufficient_credit"
    return ""


def session_fee_pence(equipment: Equipment, session: UsageSession) -> int:
    """The fee for a finished session: session time, at least the minimum charge, rounded to the penny."""
    return _fee(equipment, session.reason, session.removed, session.duration_seconds)


def _fee(equipment: Equipment, reason, removed, duration_seconds) -> int:
    if removed or reason in UNCHARGED_REASONS or not equipment.usage_cost_pence_per_hour:
        return 0
    seconds = max(duration_seconds, minimum_charge_seconds())
    # Integer maths, rounding half up: rate is pence per hour.
    return (equipment.usage_cost_pence_per_hour * seconds + 1800) // 3600


def fee_so_far_pence(session: UsageSession, now=None) -> int:
    """What an open session would cost if it ended now (at least the minimum charge). Spec 09 §9.5."""
    now = now or timezone.now()
    seconds = max(0, int((now - session.started_at).total_seconds()))
    return _fee(session.equipment, session.reason, False, seconds)


def live_balance_pence(member, session: UsageSession | None = None, now=None) -> int:
    """Credit minus the session's fee, until that fee has been charged (spec 09 §9.5)."""
    balance = member_credit_pence(member)
    if session is not None and session.charged_at is None:
        if session.is_open:
            balance -= fee_so_far_pence(session, now)
        else:
            balance -= session.fee_pence or 0
    return balance


def open_session(equipment: Equipment):
    return equipment.sessions.filter(ended_at__isnull=True).order_by("-started_at").first()


@transaction.atomic
def start_session(equipment: Equipment, member, fob=None, *, close_existing: bool) -> UsageSession:
    """Start a session. Assumes machine_refusal_reason() said yes.

    close_existing=True is the legacy (v1) behaviour: a new tap ends whatever session was open on the machine.
    v2 refuses a second member instead (decided 2026-11-07), so it passes False after checking.
    """
    current = open_session(equipment)
    if current is not None and close_existing:
        end_session(current, ended_by=UsageSession.EndedBy.DEVICE)
    reason = UsageSession.Reason.NORMAL
    if equipment.status == Equipment.Status.OUT_OF_ORDER:
        reason = UsageSession.Reason.MAINTENANCE  # only the team can get here (05 §5.4)
    now = timezone.now()
    return UsageSession.objects.create(
        equipment=equipment, member=member, fob=fob, started_at=now, last_heartbeat_at=now, reason=reason
    )


def heartbeat(session: UsageSession):
    session.last_heartbeat_at = timezone.now()
    session.save(update_fields=["last_heartbeat_at"])


@transaction.atomic
def end_session(session: UsageSession, *, when=None, ended_by: str) -> UsageSession:
    """End a session and charge its fee to the member's credit straight away (05 §5.4)."""
    session.ended_at = when or timezone.now()
    session.ended_by = ended_by
    session.removed = (session.ended_at - session.started_at) <= SHORT_SESSION
    session.fee_pence = session_fee_pence(session.equipment, session)
    session.save(update_fields=["ended_at", "ended_by", "removed", "fee_pence"])
    services.settle_session(session)
    return session


def running_seconds(equipment: Equipment, since=None, until=None) -> int:
    """How long the machine has run in a range (spec 09 §9.4). Worked out each time, so it can't drift."""
    if equipment.meter_mode == Equipment.MeterMode.CURRENT:
        readings = equipment.meter_readings.all()
        if since:
            readings = readings.filter(received_at__gte=since)
        if until:
            readings = readings.filter(received_at__lte=until)
        return readings.aggregate(total=Sum("active_seconds"))["total"] or 0
    now = timezone.now()
    total = 0
    sessions = equipment.sessions.filter(removed=False)
    if since:
        sessions = sessions.exclude(ended_at__lt=since)
    if until:
        sessions = sessions.filter(started_at__lte=until)
    for session in sessions:
        start = max(session.started_at, since) if since else session.started_at
        end = session.ended_at or now
        if until:
            end = min(end, until)
        total += max(0, int((end - start).total_seconds()))
    return total


def hours_since_service(equipment: Equipment) -> float:
    last = equipment.service_records.order_by("-serviced_at", "-created_at").first()
    since = None
    if last is not None:
        since = timezone.make_aware(datetime.combine(last.serviced_at, time.min))
    return running_seconds(equipment, since=since) / 3600


def service_due(equipment: Equipment) -> bool:
    return bool(equipment.service_interval_hours) and hours_since_service(equipment) >= equipment.service_interval_hours


def check_service_reminders():
    """Tell the team once when a machine becomes due for service; recording a service re-arms it."""
    sent = 0
    for equipment in Equipment.objects.filter(archived=False, service_interval_hours__isnull=False):
        if equipment.service_reminder_sent_at is None and service_due(equipment):
            notify_team(equipment, f"{equipment.name} is due for a service.")
            Equipment.objects.filter(pk=equipment.pk).update(service_reminder_sent_at=timezone.now())
            sent += 1
    return sent


def auto_end_idle_sessions(now=None) -> int:
    """End sessions on current-draw machines whose motor stopped long ago (05 §5.4, forgotten tap-outs).

    The session ends at the last moment the motor was running, not when we noticed.
    """
    now = now or timezone.now()
    ended = 0
    machines = Equipment.objects.filter(
        meter_mode=Equipment.MeterMode.CURRENT, idle_auto_end_minutes__isnull=False, archived=False
    )
    for equipment in machines:
        with transaction.atomic():
            locked = Equipment.objects.select_for_update().get(pk=equipment.pk)
            session = open_session(locked)
            if session is None:
                continue
            last_running = (
                locked.meter_readings.filter(active_seconds__gt=0, received_at__gt=session.started_at)
                .order_by("-received_at")
                .first()
            )
            last_moment = last_running.received_at if last_running else session.started_at
            if now - last_moment >= timedelta(minutes=locked.idle_auto_end_minutes):
                end_session(session, when=last_moment, ended_by=UsageSession.EndedBy.IDLE)
                ended += 1
    return ended


@transaction.atomic
def tidy_up(now=None) -> dict:
    """The hourly tidy-up (05 §5.6). Returns counts, for the job's log."""
    now = now or timezone.now()
    counts = {"closed": 0, "merged": 0, "idle": 0, "reminders": 0}
    counts["idle"] = auto_end_idle_sessions(now)
    counts["reminders"] = check_service_reminders()

    # 1. Close open sessions that have gone quiet, at their last heartbeat (or a minute after starting).
    for session in UsageSession.objects.filter(ended_at__isnull=True).select_related("equipment"):
        last_seen = session.last_heartbeat_at or session.started_at
        if now - last_seen > STALE_AFTER:
            end_at = session.last_heartbeat_at or (session.started_at + timedelta(minutes=1))
            end_session(session, when=end_at, ended_by=UsageSession.EndedBy.TIDY_UP)
            counts["closed"] += 1

    # 2. Merge back-to-back sessions by the same member on the same machine, with the same reason, so a member who
    #    taps out and straight back in pays one minimum charge, not two. Only recent sessions, and never ones the
    #    legacy system settled. The fees are put right with a correcting ledger row (settle_session).
    finished = (
        UsageSession.objects.filter(ended_at__isnull=False, ended_at__gte=now - MERGE_WINDOW)
        .exclude(legacy_id__isnull=False, charged_at__isnull=False)
        .select_related("equipment")
        .order_by("equipment_id", "member_id", "reason", "started_at")
    )
    previous = None
    for session in finished:
        same_run = (
            previous is not None
            and (previous.equipment_id, previous.member_id, previous.reason)
            == (session.equipment_id, session.member_id, session.reason)
            and session.started_at >= previous.ended_at
            and session.started_at - previous.ended_at <= MERGE_GAP
        )
        if same_run:
            previous.ended_at = session.ended_at
            previous.ended_by = session.ended_by
            previous.motor_on_seconds += session.motor_on_seconds
            previous.removed = (previous.ended_at - previous.started_at) <= SHORT_SESSION
            previous.fee_pence = session_fee_pence(previous.equipment, previous)
            previous.save(update_fields=["ended_at", "ended_by", "motor_on_seconds", "removed", "fee_pence"])
            # Keep the merged session's charges and meter readings with the session that carries on.
            session.payments.update(usage_session=previous)
            MeterReading.objects.filter(session=session).update(session=previous)
            session.delete()
            services.settle_session(previous)
            counts["merged"] += 1
        else:
            previous = session
    return counts
