"""
Attendance (spec 05 §5.10): how often members come in, worked out from the door log and machine sessions.

A **visit** is a calendar day (UK time) on which a member's fob was used: tapping in at the door (a keyholder entry
or a check-in), tapping out at the exit reader (05 §5.2: "an exit tap counts", e.g. someone let in by another member),
or starting a session on a machine. Several taps on one day count once. Refused taps don't count. Nothing extra is
stored.

The **trusted-status review list** is the members who aren't trusted yet but have come in often enough for an admin
to consider them: current members (active or honorary, not banned) with at least the "visits before review" setting,
who joined at least the "weeks a member before review" setting ago. Granting trusted status stays an admin's decision.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

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


@dataclass
class Candidate:
    member: object
    attendance: Attendance
    last_four_weeks: int


def review_list(today: date | None = None) -> list[Candidate]:
    """Members to consider for trusted status, most visits first (05 §5.10)."""
    today = today or timezone.localdate()
    config = SiteSettings.load()
    Member = get_user_model()
    members = {
        member.pk: member
        for member in Member.objects.filter(
            status__in=(Member.Status.ACTIVE, Member.Status.HONORARY),
            trusted=False,
            banned=False,
            date_joined__date__lte=today - timedelta(weeks=config.trusted_review_weeks),
        )
    }
    days = visit_days(list(members))
    four_weeks_ago = today - timedelta(weeks=4)
    found = []
    for member_id, member_days in days.items():
        if len(member_days) >= config.trusted_review_visits:
            record = Attendance(sorted(member_days, reverse=True))
            found.append(Candidate(members[member_id], record, record.since(four_weeks_ago)))
    found.sort(key=lambda c: (-c.attendance.visits, c.member.given_name, c.member.family_name))
    return found
