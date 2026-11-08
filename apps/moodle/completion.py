"""
Course completion (spec 06 §6.12, "Checking completion").

BBMS asks Moodle, and **stores** what it learns as a `TrainingCompletion`. Machine access and sign-off use only the
stored record (`equipment.rules`), never a live call, so a Moodle outage can't block a machine. Once recorded, a
completion stays until an admin clears it; a cleared one only comes back if Moodle reports the course finished again
after it was cleared.

BBMS checks: when a member presses "check now", when a trainer opens the sign-off list, and every 15 minutes for the
members waiting for sign-off (`manage.py check_moodle_completions`).
"""

import logging
from dataclasses import dataclass

from django.utils import timezone

from apps.equipment.models import Equipment, Induction, TrainingCompletion
from apps.notifications.notify import notify

from . import client as moodle
from .models import MoodleAccount

logger = logging.getLogger(__name__)

# What a check found.
COMPLETED = "completed"  # newly recorded
ALREADY = "already"  # BBMS already has it
NOT_YET = "not-yet"
NO_ACCOUNT = "no-account"  # Moodle doesn't know them yet (they haven't logged in there)
UNAVAILABLE = "unavailable"  # Moodle couldn't be asked


def has_completed(member, course_id) -> bool:
    """The stored record: the only thing machines and sign-off look at."""
    return TrainingCompletion.objects.filter(
        member=member, moodle_course_id=course_id, cleared_at__isnull=True
    ).exists()


def check(member, course_id, now=None) -> str:
    """Ask Moodle whether this member has finished this course, and record it if so."""
    now = now or timezone.now()
    existing = TrainingCompletion.objects.filter(member=member, moodle_course_id=course_id).first()
    if existing and existing.counts:
        return ALREADY
    api = moodle.client()
    try:
        account = MoodleAccount.objects.filter(member=member).first()
        if account is None:
            user_id = api.find_user_id(member)
            if user_id is None:
                return NO_ACCOUNT
            account = MoodleAccount.objects.create(member=member, moodle_user_id=user_id, found_at=now)
        status = api.course_status(account.moodle_user_id, course_id)
    except moodle.MoodleError as error:
        logger.warning("Checking course %s for member %s failed: %s", course_id, member.pk, error)
        return UNAVAILABLE
    if not status.completed:
        return NOT_YET
    finished = status.completed_at or now
    if existing is not None:  # cleared earlier: only a completion since then counts
        if existing.cleared_at and finished <= existing.cleared_at:
            return NOT_YET
        existing.completed_at, existing.last_checked_at = finished, now
        existing.cleared_at = existing.cleared_by = None
        existing.save()
    else:
        TrainingCompletion.objects.create(
            member=member, moodle_course_id=course_id, completed_at=finished, last_checked_at=now
        )
    machines = ", ".join(Equipment.objects.filter(moodle_course_id=course_id).values_list("name", flat=True))
    notify(
        member,
        f"You've finished the online course for {machines}. A trainer can now sign you off.",
        kind="course_completed",
        key=f"moodle-course-{course_id}-{int(finished.timestamp())}",
    )
    return COMPLETED


@dataclass
class Summary:
    checked: int = 0
    completed: int = 0
    unavailable: bool = False


def waiting_for_sign_off():
    """(member, course id) for everyone who has paid for training on a machine with a course, hasn't been signed off,
    and has no stored completion."""
    seen = set()
    for equipment in Equipment.objects.filter(moodle_course_id__isnull=False, archived=False):
        waiting = Induction.objects.filter(
            category=equipment.induction_key, paid=True, trained_at__isnull=True
        ).select_related("member")
        for induction in waiting:
            pair = (induction.member_id, equipment.moodle_course_id)
            if pair in seen:
                continue
            seen.add(pair)
            if not has_completed(induction.member, equipment.moodle_course_id):
                yield induction.member, equipment.moodle_course_id


def check_waiting(now=None, limit=None) -> Summary:
    """Check everyone waiting for sign-off. Stops at the first time Moodle can't be reached, so an outage costs one
    timeout rather than one per member."""
    summary = Summary()
    for member, course_id in waiting_for_sign_off():
        if limit is not None and summary.checked >= limit:
            break
        outcome = check(member, course_id, now)
        if outcome == UNAVAILABLE:
            summary.unavailable = True
            break
        summary.checked += 1
        summary.completed += outcome == COMPLETED
    return summary
