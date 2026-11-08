"""
Who's in the space now, and what happened on a day (spec 06 §6.7).

**In the space** (a best estimate: tapping out is a courtesy):
- from a member's entry tap (a keyholder opening the door, or a check-in) until they tap the exit reader;
- unless they've had no fob activity (a door tap, or using a machine) for the timeout in Settings → Who's in;
- and everyone counts as gone at the daily reset hour (4am by default);
- someone using a machine now is in, even if they came in without tapping (with another member, say).

**A day's activity** lists the door taps that let someone in or out (entries, check-ins, exits; refusals aren't
shown) and the machine sessions started that day. Members may see who was in and when (agreed policy).
"""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone

from apps.equipment.models import UsageSession
from apps.site_settings.models import SiteSettings

from .models import AccessEvent

IN_KINDS = (AccessEvent.Kind.ENTRY, AccessEvent.Kind.CHECK_IN)
SHOWN_KINDS = (*IN_KINDS, AccessEvent.Kind.EXIT)


@dataclass
class Presence:
    member: object
    since: datetime | None = None  # their entry tap (None if they're only known from a machine)
    last_seen: datetime | None = None
    using: list = field(default_factory=list)  # names of machines they're using now


def reset_time(now=None) -> datetime:
    """When everyone was last counted as gone: the most recent reset hour, UK time."""
    now = timezone.localtime(now or timezone.now())
    hour = SiteSettings.load().occupancy_reset_hour
    reset = timezone.make_aware(datetime.combine(now.date(), time(hour)))
    return reset if reset <= now else reset - timedelta(days=1)


def in_space(now=None) -> list[Presence]:
    """Members in the space now, most recently arrived first."""
    now = now or timezone.now()
    config = SiteSettings.load()
    since = reset_time(now)
    gone_before = now - timedelta(hours=config.occupancy_timeout_hours)
    people: dict[int, Presence] = {}
    last_exit = {}
    taps = (
        AccessEvent.objects.filter(kind__in=SHOWN_KINDS, member__isnull=False, occurred_at__gte=since)
        .filter(occurred_at__lte=now)
        .select_related("member")
        .order_by("occurred_at")
    )
    for tap in taps:
        presence = people.setdefault(tap.member_id, Presence(tap.member))
        presence.last_seen = tap.occurred_at
        if tap.kind == AccessEvent.Kind.EXIT:
            last_exit[tap.member_id] = tap.occurred_at
        elif presence.since is None or last_exit.get(tap.member_id, presence.since) > presence.since:
            presence.since = tap.occurred_at  # first tap in, or the first since they last tapped out
    sessions = (
        UsageSession.objects.filter(removed=False, started_at__lte=now)
        .filter(Q(ended_at__isnull=True, started_at__gte=since) | Q(ended_at__gte=since))
        .select_related("member", "equipment")
    )
    for session in sessions:
        presence = people.setdefault(session.member_id, Presence(session.member))
        open_now = session.ended_at is None
        seen = now if open_now else max(filter(None, (session.started_at, session.last_heartbeat_at, session.ended_at)))
        presence.last_seen = max(filter(None, (presence.last_seen, seen)))
        if open_now:
            presence.using.append(session.equipment.name)
    found = []
    for member_id, presence in people.items():
        tapped_out = member_id in last_exit and (presence.since is None or last_exit[member_id] >= presence.since)
        if presence.using:
            found.append(presence)
        elif presence.since and not tapped_out and presence.last_seen >= gone_before:
            found.append(presence)
    return sorted(found, key=lambda p: p.since or p.last_seen, reverse=True)


def count(now=None) -> int:
    return len(in_space(now))


@dataclass
class Moment:
    at: datetime
    member: object
    what: str
    offline: bool = False
    minutes: int | None = None


TAP_WORDS = {
    AccessEvent.Kind.ENTRY: "Opened the front door",
    AccessEvent.Kind.CHECK_IN: "Checked in at the door",
    AccessEvent.Kind.EXIT: "Tapped out",
}


def day(date) -> list[Moment]:
    """What happened on a day (UK time), newest first."""
    start = timezone.make_aware(datetime.combine(date, time.min))
    end = start + timedelta(days=1)
    moments = [
        Moment(tap.occurred_at, tap.member, TAP_WORDS[tap.kind], offline=tap.delayed)
        for tap in AccessEvent.objects.filter(
            kind__in=SHOWN_KINDS, member__isnull=False, occurred_at__gte=start, occurred_at__lt=end
        ).select_related("member")
    ]
    for session in UsageSession.objects.filter(removed=False, started_at__gte=start, started_at__lt=end).select_related(
        "member", "equipment"
    ):
        minutes = None
        if session.ended_at:
            minutes = max(1, round((session.ended_at - session.started_at).total_seconds() / 60))
        verb = "Used" if session.ended_at else "Using"
        moments.append(
            Moment(session.started_at, session.member, f"{verb} the {session.equipment.name}", minutes=minutes)
        )
    return sorted(moments, key=lambda m: m.at, reverse=True)
