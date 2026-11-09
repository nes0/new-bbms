"""
Membership statistics (spec 06 §6.13): totals by status now, joiners and leavers, and members month by month.

From cutover BBMS records every status change (MemberStatusChange) and a nightly snapshot of the totals
(MembershipSnapshot), so those months are exact. Legacy kept no history, so months before the first snapshot are
**estimated** from imported paid subscription charges: members who paid in a month, joiners in the month of their
first payment, and leavers (members who have left) in the month after their last payment.
"""

from datetime import date

from django.db.models import Count, Max, Min
from django.db.models.functions import TruncMonth
from django.utils import timezone

from .models import Member, MembershipSnapshot, MemberStatusChange

S = Member.Status
COUNTED = (S.ACTIVE, S.LEAVING)  # "members" in the trend, as on the stats page
SHOWN = (S.ACTIVE, S.LEAVING, S.HONORARY, S.SUSPENDED, S.SETTING_UP)
JOINED_FROM = (S.SETTING_UP, "")  # a member becoming active (or honorary) from setting up is a joiner
JOINED_TO = (S.ACTIVE, S.HONORARY)
MONTHS = 24


def counts_now() -> dict:
    rows = Member.objects.values("status").annotate(n=Count("pk"))
    return {row["status"]: row["n"] for row in rows}


def take_snapshot(day: date | None = None) -> MembershipSnapshot:
    """Record today's totals (the nightly job). Taking it again the same day updates it."""
    day = day or timezone.localdate()
    snapshot, _ = MembershipSnapshot.objects.update_or_create(
        date=day, defaults={"counts": counts_now(), "taken_at": timezone.now()}
    )
    return snapshot


def _month(day) -> date:
    return date(day.year, day.month, 1)


def _next(month: date) -> date:
    return date(month.year + month.month // 12, month.month % 12 + 1, 1)


def _previous(month: date) -> date:
    return date(month.year - (month.month == 1), 12 if month.month == 1 else month.month - 1, 1)


def _per_month(query, field="changed_at") -> dict:
    rows = query.annotate(m=TruncMonth(field)).values("m").annotate(n=Count("member", distinct=True))
    return {_month(row["m"]): row["n"] for row in rows}


def recorded_joins():
    return MemberStatusChange.objects.filter(old_status__in=JOINED_FROM, new_status__in=JOINED_TO)


def recorded_leaves():
    return MemberStatusChange.objects.filter(new_status=S.LEFT)


def now(today: date | None = None) -> dict:
    """Totals by status, and joiners and leavers this month."""
    today = today or timezone.localdate()
    counts = counts_now()
    start = _month(today)
    return {
        "by_status": [(S(status).label, counts.get(status, 0)) for status in SHOWN],
        "members": sum(counts.get(status, 0) for status in COUNTED),
        "joined": recorded_joins().filter(changed_at__date__gte=start).values("member").distinct().count(),
        "left": recorded_leaves().filter(changed_at__date__gte=start).values("member").distinct().count(),
    }


def _estimates():
    """From paid subscription charges: members paying each month, joiners and leavers per month."""
    from apps.billing.models import Charge

    paid = Charge.objects.filter(kind=Charge.Kind.SUBSCRIPTION, status=Charge.Status.PAID)
    paying = _per_month(paid, "charge_date")
    spans = paid.values("member").annotate(
        first=Min("charge_date"), last=Max("charge_date"), status=Max("member__status")
    )
    joined, left = {}, {}
    for row in spans:
        first = _month(row["first"])
        joined[first] = joined.get(first, 0) + 1
        if row["status"] == S.LEFT:
            gone = _next(_month(row["last"]))
            left[gone] = left.get(gone, 0) + 1
    return paying, joined, left


def trend(today: date | None = None, months: int = MONTHS) -> list[dict]:
    """Members (active or leaving) at the end of each month, with joiners and leavers, oldest first. Months before
    BBMS took its first snapshot are marked `estimated`."""
    today = today or timezone.localdate()
    current = _month(today)
    first_snapshot = MembershipSnapshot.objects.order_by("date").values_list("date", flat=True).first()
    recorded_from = _month(first_snapshot) if first_snapshot else current
    latest = {}
    for snap in MembershipSnapshot.objects.order_by("date"):
        latest[_month(snap.date)] = snap.counts  # the last snapshot in each month
    paying, est_joined, est_left = _estimates()
    joins, leaves = _per_month(recorded_joins()), _per_month(recorded_leaves())

    start = current
    for _ in range(months - 1):
        start = _previous(start)
    earliest = min([*paying, recorded_from])
    start = max(start, earliest)

    rows, month = [], start
    live = counts_now()
    while month <= current:
        estimated = month < recorded_from
        if month == current:
            counts = live
        else:
            counts = latest.get(month)
        if counts is not None and not estimated:
            members = sum(counts.get(status, 0) for status in COUNTED)
        else:
            members = paying.get(month, 0)
        rows.append(
            {
                "month": month.isoformat(),
                "label": f"{month:%b %Y}",
                "members": members,
                "joined": (est_joined if estimated else joins).get(month, 0),
                "left": (est_left if estimated else leaves).get(month, 0),
                "estimated": estimated,
            }
        )
        month = _next(month)
    return rows
