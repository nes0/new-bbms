"""
Who gets in: fob lookup and door/exit decisions (spec 05 §5.1–5.2).

Kept separate from the web views so the same rules serve the v1 API (today's firmware), the v2 API and the tests.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone

from . import tags
from .models import AccessEvent, Device, KeyFob

# A device-reported time this far in the past marks an event as delayed (05 §5.4).
DELAYED_AFTER = timedelta(minutes=1)
# Repeated taps of the same fob on the same reader within this time are recorded once. Today's door firmware
# sends up to 13 lookups per tap for a fob it hasn't cached, and with card-number matching they all match.
REPEAT_WINDOW = timedelta(seconds=10)


def find_fob(raw_tag: str) -> KeyFob | None:
    """Find an active fob from what a reader sent (05 §5.1).

    1. the full tag, exactly;
    2. for a valid 12-hex tag, its card number (fobs enrolled from a decimal-only reader have no full tag,
       and today's door firmware guesses the vendor prefix);
    3. the legacy "BB" + first 10 characters rule, for very old fob records.
    No partial matching.
    """
    tag = tags.normalise(raw_tag)
    if not tag:
        return None
    active = KeyFob.objects.filter(active=True).select_related("member")

    fob = active.filter(tag_id=tag).first()
    if fob:
        return fob
    card = tags.card_number(tag)
    if card is not None:
        fob = active.filter(card_number=card).first()
        if fob:
            return fob
    return active.filter(tag_id=("BB" + tag)[:12]).first()


class Outcome:
    """What happened at a door or exit reader."""

    OPEN = "open"  # keyholder: open the door
    CHECKED_IN = "checked_in"  # current member who isn't a keyholder: visit recorded, door stays shut
    CHECKED_OUT = "checked_out"  # exit reader
    REFUSED = "refused"


@dataclass
class Decision:
    outcome: str
    reason: str = ""
    fob: KeyFob | None = None

    @property
    def member(self):
        return self.fob.member if self.fob else None

    @property
    def allowed(self):
        return self.outcome in (Outcome.OPEN, Outcome.CHECKED_OUT)


def door_refusal_reason(member) -> str:
    """Why this member's fob doesn't open the front door, or "" if it does (05 §5.2)."""
    if not member.has_space_access:
        return "not_member"
    if not (member.trusted and member.key_holder):
        return "not_keyholder"
    if not (member.photo_approved or member.photo_on_wall):
        return "no_photo"
    return ""


def decide(device: Device, raw_tag: str, occurred_at=None) -> Decision:
    """Decide what a tap at an entry or exit reader means, and record it."""
    now = timezone.now()
    # Trust the device's time only if it's in the past (a fast device clock mustn't create future events).
    when = min(occurred_at, now) if occurred_at else now
    delayed = (now - when) > DELAYED_AFTER

    fob = find_fob(raw_tag)
    if fob is None:
        # Unknown fob. Not logged: today's door firmware sends up to 13 guesses per tap for an unknown fob.
        return Decision(Outcome.REFUSED, "unknown_fob")

    member = fob.member
    if device.role == Device.Role.EXIT:
        if not member.has_space_access:
            decision = Decision(Outcome.REFUSED, "not_member", fob)
        else:
            decision = Decision(Outcome.CHECKED_OUT, "", fob)
    else:
        reason = door_refusal_reason(member)
        if not reason:
            decision = Decision(Outcome.OPEN, "", fob)
        elif member.has_space_access:
            # A current member who just can't open the door: their tap counts as a visit (decided 2026-11-07).
            decision = Decision(Outcome.CHECKED_IN, reason, fob)
        else:
            decision = Decision(Outcome.REFUSED, reason, fob)

    kind = {
        Outcome.OPEN: AccessEvent.Kind.ENTRY,
        Outcome.CHECKED_IN: AccessEvent.Kind.CHECK_IN,
        Outcome.CHECKED_OUT: AccessEvent.Kind.EXIT,
        Outcome.REFUSED: AccessEvent.Kind.REFUSED,
    }[decision.outcome]
    already_recorded = AccessEvent.objects.filter(
        fob=fob, device=device, kind=kind, received_at__gte=now - REPEAT_WINDOW
    ).exists()
    if already_recorded:
        return decision
    AccessEvent.objects.create(
        kind=kind,
        member=member,
        fob=fob,
        device=device,
        reason=decision.reason,
        occurred_at=when,
        received_at=now,
        delayed=delayed,
    )
    return decision
