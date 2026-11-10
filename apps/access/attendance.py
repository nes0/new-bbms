"""
Attendance (spec 05 §5.10): how often members come in, and for how long, worked out from the door log and machine
sessions.

A **visit** is a calendar day (UK time) on which a member's fob was used: tapping in at the door (a keyholder entry
or a check-in), tapping out at the exit reader (05 §5.2: "an exit tap counts", e.g. someone let in by another member),
or starting a session on a machine. Several taps on one day count once. Refused taps don't count. Nothing extra is
stored.

**Hours in** are the same best estimate as "Who's in" (06 §6.7): a stay runs from a member's first fob activity until
they tap out, or until the "count a member as gone" timeout after their last activity if they don't, and never past
the daily reset. Tapping out is a courtesy, so this is an estimate.

The **trust and key review** (decided 2026-11-10, 01 §1.7 Q14) lists current members who don't hold a key yet, with
their visits, their hours in, the trusted members who were in the space at the same time (who will have seen them
at work), and the checks a key needs. There's no threshold: whether to trust someone and give them a key is an
admin's decision once the checks are met.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.equipment.models import UsageSession
from apps.site_settings.models import SiteSettings

from .models import AccessEvent

VISIT_KINDS = (AccessEvent.Kind.ENTRY, AccessEvent.Kind.CHECK_IN, AccessEvent.Kind.EXIT)
RECENT = 10  # how many recent visit dates are shown


@dataclass
class Attendance:
    days: list[date] = field(default_factory=list)  # newest first

    @property
    def visits(self) -> int:
        return len(self.days)

    @property
    def first(self) -> date | None:
        return self.days[-1] if self.days else None

    @property
    def last(self) -> date | None:
        return self.days[0] if self.days else None

    @property
    def recent(self) -> list[date]:
        return self.days[:RECENT]

    def since(self, day: date) -> int:
        """Visits on or after this day."""
        return sum(1 for visit in self.days if visit >= day)


def visit_days(member_ids=None) -> dict[int, set[date]]:
    """Each member's visit days. `member_ids` limits it to those members (None: everyone)."""
    uk = timezone.get_current_timezone()
    taps = AccessEvent.objects.filter(kind__in=VISIT_KINDS, member__isnull=False)
    sessions = UsageSession.objects.all()
    if member_ids is not None:
        taps = taps.filter(member_id__in=member_ids)
        sessions = sessions.filter(member_id__in=member_ids)
    days = defaultdict(set)
    for rows in (
        taps.annotate(day=TruncDate("occurred_at", tzinfo=uk)),
        sessions.annotate(day=TruncDate("started_at", tzinfo=uk)),
    ):
        for member_id, day in rows.order_by().values_list("member_id", "day").distinct():
            days[member_id].add(day)
    return days


def attendance(member) -> Attendance:
    return Attendance(sorted(visit_days([member.pk]).get(member.pk, ()), reverse=True))


# --- Hours in, and time in the space with trusted members ---

Stay = tuple[datetime, datetime]


def _next_reset(moment: datetime, hour: int) -> datetime:
    """The first daily reset after `moment` (UK time)."""
    local = timezone.localtime(moment)
    reset = timezone.make_aware(datetime.combine(local.date(), time(hour)))
    return (
        reset if reset > moment else timezone.make_aware(datetime.combine(local.date() + timedelta(days=1), time(hour)))
    )


def stays(member_ids, now=None) -> dict[int, list[Stay]]:
    """Each member's estimated stays in the space, oldest first (the "Who's in" estimate, 06 §6.7)."""
    now = now or timezone.now()
    config = SiteSettings.load()
    timeout, reset_hour = timedelta(hours=config.occupancy_timeout_hours), config.occupancy_reset_hour
    points = defaultdict(list)  # member id -> [(when, is_exit)]
    taps = AccessEvent.objects.filter(kind__in=VISIT_KINDS, member_id__in=member_ids, occurred_at__lte=now)
    for member_id, when, kind in taps.order_by().values_list("member_id", "occurred_at", "kind"):
        points[member_id].append((when, kind == AccessEvent.Kind.EXIT))
    sessions = UsageSession.objects.filter(member_id__in=member_ids, removed=False, started_at__lte=now)
    for member_id, started, heartbeat, ended in sessions.order_by().values_list(
        "member_id", "started_at", "last_heartbeat_at", "ended_at"
    ):
        points[member_id].append((started, False))
        points[member_id].append((min(now, ended or max(filter(None, (started, heartbeat, now)))), False))
    found = {}
    for member_id, member_points in points.items():
        found[member_id] = _join(sorted(member_points), timeout, reset_hour, now)
    return found


def _join(points, timeout, reset_hour, now) -> list[Stay]:
    found, start, last = [], None, None

    def close(at):
        if at > start:
            found.append((start, at))

    for when, is_exit in points:
        if start is not None:
            past_reset = when >= _next_reset(last, reset_hour)
            # Tapping out shows they were still in, however long since their last tap (until the reset).
            if past_reset or (when - last > timeout and not is_exit):
                close(min(last + timeout, _next_reset(last, reset_hour)))  # gone without tapping out
                start = None
        if is_exit:
            if start is not None:
                close(when)
                start = None
            continue
        if start is None:
            start = when
        last = when
    if start is not None:
        close(min(last + timeout, _next_reset(last, reset_hour), now))
    return found


def hours_in(member_stays: list[Stay]) -> float:
    return sum((end - start).total_seconds() for start, end in member_stays) / 3600


def _overlap(mine: list[Stay], theirs: list[Stay]) -> float:
    """Hours both were in. Both lists are oldest first and a member's stays don't overlap each other."""
    seconds, i, j = 0.0, 0, 0
    while i < len(mine) and j < len(theirs):
        start, end = max(mine[i][0], theirs[j][0]), min(mine[i][1], theirs[j][1])
        if end > start:
            seconds += (end - start).total_seconds()
        if mine[i][1] < theirs[j][1]:
            i += 1
        else:
            j += 1
    return seconds / 3600


# --- The trust and key review (05 §5.10) ---

SHOWN_TRUSTED = 5  # trusted members named for each candidate; the rest are counted


@dataclass
class SeenWith:
    member: object
    hours: float


@dataclass
class Candidate:
    member: object
    attendance: Attendance
    last_four_weeks: int
    hours: float
    seen_with: list[SeenWith]  # trusted members who were in at the same time, most hours first
    checks: list[tuple[str, bool]]  # (what a key needs, whether it's done), in the order they're usually done

    @property
    def trusted_count(self) -> int:
        return len(self.seen_with)

    @property
    def shown_trusted(self) -> list[SeenWith]:
        return self.seen_with[:SHOWN_TRUSTED]

    @property
    def more_trusted(self) -> int:
        return max(0, len(self.seen_with) - SHOWN_TRUSTED)

    @property
    def ready(self) -> bool:
        return all(done for _, done in self.checks)


def key_checks(member, *, has_fob: bool, deposit_paid: bool) -> list[tuple[str, bool]]:
    """What a member needs before they're given a key (05 §5.2). Trust itself is the admin's decision."""
    return [
        ("Email confirmed", member.email_verified),
        ("ID checked", member.id_checked),
        ("General induction", member.induction_confirmed),
        ("Photo", member.photo_approved or member.photo_on_wall),
        ("Fob", has_fob),
        ("Trusted", member.trusted),
        ("Key deposit", deposit_paid),
    ]


def review_list(today: date | None = None, now=None) -> list[Candidate]:
    """Current members without a key who have visited at least once, most hours in first (05 §5.10)."""
    from apps.payments.models import Payment

    from .models import KeyFob

    today = today or timezone.localdate()
    Member = get_user_model()
    members = {
        member.pk: member
        for member in Member.objects.filter(
            status__in=(Member.Status.ACTIVE, Member.Status.HONORARY), key_holder=False, banned=False
        ).prefetch_related("id_checks")
    }
    days = visit_days(list(members))
    trusted = {member.pk: member for member in Member.objects.filter(trusted=True)}
    all_stays = stays({*days, *trusted}, now)
    with_fobs = set(KeyFob.objects.filter(member_id__in=days, active=True).values_list("member_id", flat=True))
    deposits = set(
        Payment.objects.filter(
            member_id__in=days,
            reason=Payment.Reason.KEY_DEPOSIT,
            status__in=(Payment.Status.PAID, Payment.Status.PENDING),
        ).values_list("member_id", flat=True)
    )
    four_weeks_ago = today - timedelta(weeks=4)
    found = []
    for member_id, member_days in days.items():
        member, mine = members[member_id], all_stays.get(member_id, [])
        seen_with = []
        for other_id, other in trusted.items():
            if other_id != member_id and (hours := _overlap(mine, all_stays.get(other_id, []))) > 0:
                seen_with.append(SeenWith(other, hours))
        seen_with.sort(key=lambda seen: -seen.hours)
        record = Attendance(sorted(member_days, reverse=True))
        checks = key_checks(member, has_fob=member_id in with_fobs, deposit_paid=member_id in deposits)
        found.append(Candidate(member, record, record.since(four_weeks_ago), hours_in(mine), seen_with, checks))
    found.sort(key=lambda c: (-c.hours, c.member.given_name, c.member.family_name))
    return found
