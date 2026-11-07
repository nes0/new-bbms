"""
Who may use a machine, usage sessions and their fees (spec 05 §5.3, §5.6).

Plain functions, so the v1 API, the v2 API, the admin and the scheduled tidy-up all share one set of rules.
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import Equipment, Induction, TrainingCompletion, UsageSession

# TODO(settings app): these become admin settings (spec 02 §2.8).
MINIMUM_CHARGE = timedelta(minutes=15)
ALLOWED_NEGATIVE_CREDIT_PENCE = 500  # credit must be above -£5 to start a session
SHORT_SESSION = timedelta(seconds=60)  # this short or less: removed, not charged
MERGE_GAP = timedelta(seconds=60)  # back-to-back sessions this close are merged
STALE_AFTER = timedelta(hours=1)  # an open session with no heartbeat for this long is closed

UNCHARGED_REASONS = (UsageSession.Reason.TRAINING, UsageSession.Reason.TESTING, UsageSession.Reason.MAINTENANCE)


def member_credit_pence(member) -> int:
    # TODO(payments app): the member's credit, worked out from the ledger (04 §4.7).
    return 0


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


def machine_refusal_reason(member, equipment: Equipment) -> str:
    """Why this member can't start a session on this machine, or "" if they can (05 §5.3)."""
    if not member.has_space_access:
        return "not_member"
    on_team = is_on_team(member, equipment)
    if equipment.status == Equipment.Status.OUT_OF_ORDER and not on_team:
        return "out_of_order"
    if equipment.requires_induction:
        induction = Induction.objects.filter(member=member, category=equipment.induction_key).first()
        if induction is None or not induction.is_trained:
            return "not_trained"
        reason = _course_reason(member, equipment, induction)
        if reason:
            return reason
    if member_credit_pence(member) + ALLOWED_NEGATIVE_CREDIT_PENCE <= 0 and equipment.usage_cost_pence_per_hour:
        return "insufficient_credit"
    return ""


def session_fee_pence(equipment: Equipment, session: UsageSession) -> int:
    """The fee for a finished session: session time, at least the minimum charge, rounded to the penny."""
    if session.removed or session.reason in UNCHARGED_REASONS or not equipment.usage_cost_pence_per_hour:
        return 0
    seconds = max(session.duration_seconds, int(MINIMUM_CHARGE.total_seconds()))
    # Integer maths, rounding half up: rate is pence per hour.
    return (equipment.usage_cost_pence_per_hour * seconds + 1800) // 3600


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


def end_session(session: UsageSession, *, when=None, ended_by: str) -> UsageSession:
    """End a session and work out its fee straight away (charging: payments app)."""
    session.ended_at = when or timezone.now()
    session.ended_by = ended_by
    session.removed = (session.ended_at - session.started_at) <= SHORT_SESSION
    session.fee_pence = session_fee_pence(session.equipment, session)
    session.save(update_fields=["ended_at", "ended_by", "removed", "fee_pence"])
    # TODO(payments app): take fee_pence from the member's credit and set charged_at.
    return session


@transaction.atomic
def tidy_up(now=None) -> dict:
    """The hourly tidy-up (05 §5.6). Returns counts, for the job's log."""
    now = now or timezone.now()
    counts = {"closed": 0, "merged": 0}

    # 1. Close open sessions that have gone quiet, at their last heartbeat (or a minute after starting).
    for session in UsageSession.objects.filter(ended_at__isnull=True).select_related("equipment"):
        last_seen = session.last_heartbeat_at or session.started_at
        if now - last_seen > STALE_AFTER:
            end_at = session.last_heartbeat_at or (session.started_at + timedelta(minutes=1))
            end_session(session, when=end_at, ended_by=UsageSession.EndedBy.TIDY_UP)
            counts["closed"] += 1

    # 2. Merge back-to-back uncharged sessions by the same member on the same machine, with the same reason.
    finished = (
        UsageSession.objects.filter(ended_at__isnull=False, charged_at__isnull=True)
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
            session.delete()
            counts["merged"] += 1
        else:
            previous = session
    return counts
