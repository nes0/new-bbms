"""
What the members' equipment pages show and let people do (spec 05 §5.7, 09 §9.2 "member-facing web pages").

Plain functions, so the views stay thin and the rules can be tested on their own:
- `training_state` says where a member is with a machine's training (fee, sign-off, online course);
- `report_status` is the web page's version of the out-of-order button;
- `record_service` and `reclassify` are for the machine's team (05 §5.4, §5.6).
"""

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.notifications.notify import notify
from apps.payments import services

from . import rules
from .models import Equipment, Induction, MachineStatusChange, ServiceRecord, TrainingCompletion, UsageSession

# What a member is told when machine_refusal_reason() says no (05 §5.3).
REFUSALS = {
    "not_member": "Only members can use the machines.",
    "out_of_order": "It's out of order, so it can't be used until it's fixed.",
    "not_trained": "You need to be trained on it first.",
    "course_incomplete": "You need to finish its online course first.",
    "insufficient_credit": "You don't have enough credit. Top up before you use it.",
}


class PageError(Exception):
    """Something a member tried on an equipment page that isn't allowed; the message says why."""


def visible_equipment():
    return Equipment.objects.filter(archived=False).select_related("team")


@dataclass
class TrainingState:
    """Where a member is with a machine's training. `step` is one of:

    "none" (no training needed), "unpaid", "waiting" (paid, waiting for a trainer), "trained".
    """

    step: str
    induction: Induction | None = None
    course_needed: bool = False  # the machine has an online course this member still has to finish
    course_done: bool = False
    course_kept: bool = False  # signed off before the course existed: keeps access without it (06 §6.12)


def training_state(member, equipment: Equipment) -> TrainingState:
    if not equipment.requires_induction:
        return TrainingState("none")
    induction = Induction.objects.filter(member=member, category=equipment.induction_key).first()
    if induction is None or not induction.paid:
        step = "unpaid"
    else:
        step = "trained" if induction.is_trained else "waiting"
    state = TrainingState(step, induction)
    if equipment.moodle_course_id:
        state.course_done = TrainingCompletion.objects.filter(
            member=member, moodle_course_id=equipment.moodle_course_id, cleared_at__isnull=True
        ).exists()
        if not state.course_done:
            check = induction or Induction(member=member, category=equipment.induction_key)
            missing = rules._course_reason(member, equipment, check) != ""
            state.course_kept = step == "trained" and not missing
            state.course_needed = missing
    return state


def refusal(member, equipment: Equipment) -> str:
    """Why this member can't use the machine now, as a sentence, or "" if they can."""
    return REFUSALS.get(rules.machine_refusal_reason(member, equipment), "")


def people(equipment: Equipment) -> dict:
    """Trainers, trained members and members waiting for training (05 §5.7). Only current members are listed."""
    inductions = (
        Induction.objects.filter(category=equipment.induction_key, paid=True)
        .select_related("member")
        .order_by("member__given_name", "member__family_name")
    )
    current = [i for i in inductions if i.member.has_space_access]
    return {
        "trainers": [i.member for i in current if i.is_trainer and i.is_trained],
        "trained": [i.member for i in current if i.is_trained and not i.is_trainer],
        "waiting": [i for i in current if not i.is_trained],
    }


def usage_minutes(equipment: Equipment) -> dict:
    """Total time used, split as legacy did (05 §5.7): charged use, training, testing and maintenance."""
    finished = equipment.sessions.filter(ended_at__isnull=False, removed=False)
    totals = {"used": 0, "training": 0, "testing": 0, "maintenance": 0}
    for reason, started, ended in finished.values_list("reason", "started_at", "ended_at"):
        key = reason or "used"
        totals[key] = totals.get(key, 0) + int((ended - started).total_seconds())
    return {key: seconds // 60 for key, seconds in totals.items()}


def recent_sessions(equipment: Equipment):
    """Finished sessions, newest first (05 §5.7: removed ones are hidden)."""
    return (
        equipment.sessions.filter(ended_at__isnull=False, removed=False)
        .select_related("member")
        .order_by("-started_at")
    )


def running_hours(equipment: Equipment) -> dict:
    """Running hours for the page: total, since the last service, and whether a service is due (05 §5.4)."""
    last = equipment.service_records.order_by("-serviced_at", "-created_at").first()
    return {
        "total": rules.running_seconds(equipment) / 3600,
        "since_service": rules.hours_since_service(equipment),
        "interval": equipment.service_interval_hours,
        "due": rules.service_due(equipment),
        "last_service": last,
        "motor_on_minutes": (equipment.sessions.aggregate(total=Sum("motor_on_seconds"))["total"] or 0) // 60,
    }


def can_report(member, equipment: Equipment) -> bool:
    """Who may report a fault from the page: anyone who may use the machine, ignoring credit, and its team."""
    if rules.is_on_team(member, equipment):
        return True
    return rules.machine_refusal_reason(member, equipment, for_offline_cache=True) == ""


@transaction.atomic
def report_status(equipment: Equipment, member, status: str, note: str) -> MachineStatusChange:
    """Mark a machine out of order, or working again, from its page (05 §5.4, "Out-of-order button").

    A member who may use the machine can report it out of order, with a note saying what's wrong. Only the team
    (or an admin) can mark it working again, so a fault isn't cleared by the next person who wants to use it.
    """
    note = (note or "").strip()[:255]
    equipment = Equipment.objects.select_for_update().get(pk=equipment.pk)
    if status not in Equipment.Status.values:
        raise PageError("Choose working or out of order.")
    on_team = rules.is_on_team(member, equipment)
    if not can_report(member, equipment):
        raise PageError("Only members trained on this machine, or its team, can report on it.")
    if status == Equipment.Status.WORKING and not on_team:
        raise PageError("Only the machine's team can mark it as working again.")
    if status == Equipment.Status.OUT_OF_ORDER and not note:
        raise PageError("Say what's wrong, so the team knows what to fix.")
    if status == equipment.status:
        # Same status: only a new note, from the team or whoever reported it (05 §5.4: "the member can add a note").
        latest = equipment.status_changes.order_by("-created_at", "-pk").first()
        reporter = latest is not None and latest.reported_by_id == member.pk
        if note == equipment.status_note or not (on_team or reporter):
            raise PageError("It's already marked like that.")
    status_changed = status != equipment.status
    if status_changed:
        equipment.status_since = timezone.now()
    equipment.status, equipment.status_note = status, note
    equipment.save(update_fields=["status", "status_note", "status_since"])
    change = MachineStatusChange.objects.create(
        equipment=equipment, status=status, note=note, reported_by=member, source=MachineStatusChange.Source.WEB
    )
    if status_changed:
        label = "out of order" if status == Equipment.Status.OUT_OF_ORDER else "working again"
        message = f"{member.display_name} reported {equipment.name} {label}. {note}".strip()
    else:
        message = f"{member.display_name} says what's wrong with {equipment.name}: {note}"
    rules.notify_team(equipment, message)
    return change


def awaiting_reason(member, equipment: Equipment) -> bool:
    """This member reported the machine out of order (from its reader's button) and hasn't said why yet."""
    if equipment.status != Equipment.Status.OUT_OF_ORDER or equipment.status_note:
        return False
    latest = equipment.status_changes.order_by("-created_at", "-pk").first()
    return latest is not None and latest.reported_by_id == member.pk


def record_service(equipment: Equipment, member, serviced_at, note: str) -> ServiceRecord:
    """The team records a service: the hours-since-service count starts again and the reminder is re-armed."""
    if not rules.is_on_team(member, equipment):
        raise PageError("Only the machine's team can record a service.")
    if serviced_at > timezone.localdate():
        raise PageError("The service date can't be in the future.")
    return ServiceRecord.objects.create(
        equipment=equipment, serviced_at=serviced_at, note=(note or "").strip()[:255], recorded_by=member
    )


RECLASSIFY_TO = (UsageSession.Reason.TRAINING, UsageSession.Reason.TESTING)


@transaction.atomic
def reclassify(session: UsageSession, member, reason: str) -> UsageSession:
    """Mark a finished session as training or testing, once (05 §5.6). Any fee already taken goes back to credit as a
    new ledger row; the original charge is never edited."""
    session = UsageSession.objects.select_for_update().select_related("equipment").get(pk=session.pk)
    if not rules.is_on_team(member, session.equipment):
        raise PageError("Only the machine's team can change what a session was for.")
    if reason not in RECLASSIFY_TO:
        raise PageError("A session can be marked as training or testing.")
    if session.ended_at is None:
        raise PageError("That session hasn't finished yet.")
    if session.reason != UsageSession.Reason.NORMAL:
        raise PageError("That session has already been marked.")
    session.reason = reason
    session.fee_pence = rules.session_fee_pence(session.equipment, session)
    session.save(update_fields=["reason", "fee_pence"])
    correction = services.settle_session(session)
    if correction is not None and correction.amount_pence < 0:
        notify(
            session.member,
            f"Your {session.equipment.name} session on {timezone.localtime(session.started_at):%-d %b} was marked as "
            f"{session.get_reason_display().lower()}, so £{-correction.amount_pence / 100:.2f} went back to your "
            "credit.",
            kind="session_adjusted",
            url="/credit/",
        )
    return session
