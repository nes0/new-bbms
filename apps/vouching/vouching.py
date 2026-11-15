"""
The vouching rules (spec 05 §5.10).

- **Who can vouch:** an *active* member. They're a current member (active or honorary, not banned) whose fob was used
  (door tap, check-in, exit tap or the start of a machine session) within the "vouching: days" setting. They don't
  need to be trusted.
- **Who is waiting:** current members (active or honorary, not banned) who aren't trusted yet and have visited at
  least once. Nobody is shown themselves.
- **Score** = vouches − concerns, counting only vouchers who are still members (not left, not banned).
- When a score first reaches the threshold setting, the trustees and admins are notified, once per member, ever.
- A voucher sees only their own choices; the waiting member sees nothing; trustees see every name and the history.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Exists, Max, OuterRef, Q
from django.urls import reverse
from django.utils import timezone

from apps.access.attendance import VISIT_KINDS
from apps.access.models import AccessEvent
from apps.equipment.models import UsageSession
from apps.notifications.notify import notify_role
from apps.site_settings.models import SiteSettings

from .models import ThresholdNotice, Vouch, VouchChange

TRUSTEES_ROLE = "trustees"
NOTE_LENGTH = 1000


class CantVouch(Exception):
    """Why a vouch, concern or withdrawal wasn't recorded, in words for the member."""


def _current_statuses():
    Member = get_user_model()
    return (Member.Status.ACTIVE, Member.Status.HONORARY)


def last_fob_activity(member) -> datetime | None:
    """When the member's fob was last used: a door or exit tap that wasn't refused, or a machine session's start."""
    taps = AccessEvent.objects.filter(member=member, kind__in=VISIT_KINDS).aggregate(last=Max("occurred_at"))["last"]
    session = UsageSession.objects.filter(member=member).aggregate(last=Max("started_at"))["last"]
    return max(filter(None, (taps, session)), default=None)


def why_cant_vouch(member, now=None) -> str:
    """Empty if the member may vouch now; otherwise why not, in words for them."""
    if not member.is_authenticated or member.banned or member.status not in _current_statuses():
        return "Only current members can vouch."
    days = SiteSettings.load().vouch_active_days
    last = last_fob_activity(member)
    if last is None or last < (now or timezone.now()) - timedelta(days=days):
        return (
            f"You can vouch once you've been in the space in the last {days} days: tap your fob at the door or "
            "use a machine."
        )
    return ""


def waiting_members(viewer=None):
    """Members waiting for trusted status (visited at least once), alphabetically. Never includes `viewer`."""
    Member = get_user_model()
    visited = Q(Exists(AccessEvent.objects.filter(member=OuterRef("pk"), kind__in=VISIT_KINDS))) | Q(
        Exists(UsageSession.objects.filter(member=OuterRef("pk")))
    )
    members = Member.objects.filter(visited, status__in=_current_statuses(), banned=False, trusted=False)
    if viewer is not None and viewer.pk:
        members = members.exclude(pk=viewer.pk)
    return members.order_by("given_name", "family_name", "pk")


def is_waiting(member) -> bool:
    return waiting_members().filter(pk=member.pk).exists()


def counting():
    """Vouches that count: the voucher is still a member (hasn't left and isn't banned)."""
    Member = get_user_model()
    return Vouch.objects.filter(voucher__banned=False).exclude(voucher__status=Member.Status.LEFT)


def score(member) -> int:
    return sum(counting().filter(member=member).values_list("value", flat=True))


def my_choices(voucher) -> dict[int, Vouch]:
    """The voucher's own vouches and concerns, by member id."""
    return {vouch.member_id: vouch for vouch in Vouch.objects.filter(voucher=voucher)}


@transaction.atomic
def record(member, voucher, action: str, note: str = "", now=None) -> Vouch | None:
    """Vouch for, raise a concern about, or withdraw from a waiting member. Returns the vouch (None if withdrawn)."""
    now = now or timezone.now()
    if action not in VouchChange.Action.values:
        raise CantVouch("Choose vouch, concern or withdraw.")
    if member.pk == voucher.pk:
        raise CantVouch("You can't vouch for yourself.")
    if problem := why_cant_vouch(voucher, now):
        raise CantVouch(problem)
    if not is_waiting(member):
        raise CantVouch(f"{member.full_name} isn't waiting for trusted status.")
    note = note.strip()[:NOTE_LENGTH] if action == VouchChange.Action.CONCERN else ""
    existing = Vouch.objects.select_for_update().filter(member=member, voucher=voucher).first()
    if action == VouchChange.Action.WITHDRAW:
        if existing is None:
            return None  # nothing to take back
        existing.delete()
        VouchChange.objects.create(member=member, voucher=voucher, action=action, at=now)
        return None
    value = Vouch.Value.VOUCH if action == VouchChange.Action.VOUCH else Vouch.Value.CONCERN
    if existing and existing.value == value and existing.note == note:
        return existing  # no change: nothing to add to the history
    vouch, _ = Vouch.objects.update_or_create(
        member=member, voucher=voucher, defaults={"value": value, "note": note, "updated_at": now}
    )
    VouchChange.objects.create(member=member, voucher=voucher, action=action, note=note, at=now)
    _tell_trustees_if_reached(member, now)
    return vouch


def _tell_trustees_if_reached(member, now):
    reached = score(member)
    if reached < SiteSettings.load().vouch_threshold:
        return
    notice, created = ThresholdNotice.objects.get_or_create(member=member, defaults={"score": reached, "at": now})
    if not created:
        return  # told once already, whatever has happened since
    url = f"{reverse('admin:members_member_key_review')}#member-{member.pk}"
    notify_role(
        TRUSTEES_ROLE,
        message=f"{member.full_name} has a vouching score of {reached}: members who've seen them at work think "
        "they're ready to be trusted. Trust is still your decision.",
        admins=True,
        kind="vouch_threshold",
        key=f"{member.pk}-vouch_threshold",
        url=url,
    )


# --- What the trustees see ---


@dataclass
class Summary:
    vouches: list[Vouch] = field(default_factory=list)  # oldest first
    concerns: list[Vouch] = field(default_factory=list)
    lapsed: list[Vouch] = field(default_factory=list)  # from members who have since left or been banned
    notified_at: datetime | None = None

    @property
    def score(self) -> int:
        return len(self.vouches) - len(self.concerns)

    def __bool__(self):
        return bool(self.vouches or self.concerns or self.lapsed)


def summaries(member_ids) -> dict[int, Summary]:
    """Each member's vouches and concerns, with names, for the trust and key review and the member's admin page."""
    found = {member_id: Summary() for member_id in member_ids}
    counted = set(counting().filter(member_id__in=member_ids).values_list("pk", flat=True))
    vouches = Vouch.objects.filter(member_id__in=member_ids).select_related("voucher").order_by("created_at", "pk")
    for vouch in vouches:
        summary = found[vouch.member_id]
        if vouch.pk not in counted:
            summary.lapsed.append(vouch)
        elif vouch.value == Vouch.Value.VOUCH:
            summary.vouches.append(vouch)
        else:
            summary.concerns.append(vouch)
    for notice in ThresholdNotice.objects.filter(member_id__in=member_ids):
        found[notice.member_id].notified_at = notice.at
    return found


def history(member):
    return VouchChange.objects.filter(member=member).select_related("voucher")


def reached_threshold() -> set[int]:
    """Ids of members whose score is at or above the threshold now."""
    threshold = SiteSettings.load().vouch_threshold
    totals = {}
    for member_id, value in counting().values_list("member_id", "value"):
        totals[member_id] = totals.get(member_id, 0) + value
    return {member_id for member_id, total in totals.items() if total >= threshold}
