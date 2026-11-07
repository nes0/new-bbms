"""
Issuing fobs to members, and enrolling a new fob by tapping it on a reader (spec 05 §5.1).

Every way a fob is added goes through `issue_fob`, so the same rules always apply:
- only an active member gets a fob (FOB_STATUSES; not banned);
- a member holds at most `max_active_fobs` active fobs (a setting; lost fobs don't count);
- a tag id and a card number each belong to one fob only, lost ones included (a found fob is reactivated, not
  issued again).
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.site_settings.models import SiteSettings

from . import tags
from .models import Device, FobEnrolment, KeyFob

# The largest card number: 8 hex digits.
MAX_CARD_NUMBER = 0xFFFFFFFF


# Who can be given a new fob (decided 2026-11-07): active members. Honorary members count as active; members who are
# setting up, leaving, suspended or have left don't.
FOB_STATUSES = ("active", "honorary")


class FobError(Exception):
    """A fob can't be issued (or an enrolment opened); the message is for the admin."""


def parse_fob_id(text: str) -> tuple[str | None, int]:
    """What an admin typed or a USB reader typed in: (full tag or None, card number).

    Accepts the full 12-hex tag (as the reader shows it in hex mode; its checksum must be right), or the decimal
    card number printed on the fob (up to 10 digits).
    """
    value = tags.normalise(text)
    if len(value) == 12 and all(c in "0123456789ABCDEF" for c in value):
        if not tags.is_valid_full_tag(value):
            raise FobError(
                f"{value} isn't a valid 12-character tag: its last two characters should be "
                f"{tags.checksum(value[:10])}. Check for a typo, or enter the number printed on the fob."
            )
        return value, tags.card_number(value)
    if value.isascii() and value.isdigit() and len(value) <= 10:
        number = int(value)
        if not 0 < number <= MAX_CARD_NUMBER:
            raise FobError(f"{value} is too large to be a card number.")
        return None, number
    raise FobError("Enter the 12-character tag (hex) or the card number printed on the fob (up to 10 digits).")


def can_have_fob_reason(member) -> str:
    """Why this member can't be given another fob now, or "" if they can."""
    if member.banned:
        return f"{member} is banned."
    if member.status not in FOB_STATUSES:
        return f"{member} needs an active membership to get a fob (status: {member.get_status_display()})."
    limit = SiteSettings.load().max_active_fobs
    if member.fobs.filter(active=True).count() >= limit:
        fobs = "1 active fob" if limit == 1 else f"{limit} active fobs"
        return f"{member} already has {fobs}, the most allowed. Mark one lost first."
    return ""


def already_registered_reason(tag_id: str | None, card_number: int) -> str:
    existing = KeyFob.objects.filter(card_number=card_number).select_related("member").first()
    if existing is None and tag_id:
        existing = KeyFob.objects.filter(tag_id=tag_id).select_related("member").first()
    if existing is None:
        return ""
    state = "" if existing.active else " (marked lost: reactivate it instead)"
    return f"That fob is already registered to {existing.member}{state}."


def issue_fob(member, *, card_number: int, tag_id: str | None = None, issued_by=None) -> KeyFob:
    """Give a member a fob. Raises FobError if any rule says no."""
    with transaction.atomic():
        # Lock the member so two admins can't take them over the limit at the same moment.
        type(member).objects.select_for_update().filter(pk=member.pk).first()
        reason = can_have_fob_reason(member) or already_registered_reason(tag_id, card_number)
        if reason:
            raise FobError(reason)
        return KeyFob.objects.create(member=member, tag_id=tag_id, card_number=card_number, issued_by=issued_by)


def reactivate_fob(fob: KeyFob) -> KeyFob:
    """A lost fob turned up again."""
    with transaction.atomic():
        type(fob.member).objects.select_for_update().filter(pk=fob.member_id).first()
        reason = can_have_fob_reason(fob.member)
        if reason:
            raise FobError(reason)
        fob.active = True
        fob.lost_at = None
        fob.save(update_fields=["active", "lost_at"])
    return fob


# --- enrolment by tapping the fob on a reader ---


def waiting_enrolment(device: Device):
    """The open window on this reader, if any (one at a time)."""
    return (
        FobEnrolment.objects.filter(
            device=device,
            captured_at__isnull=True,
            cancelled_at__isnull=True,
            confirmed_at__isnull=True,
            expires_at__gt=timezone.now(),
        )
        .select_related("member", "opened_by")
        .first()
    )


def open_enrolment(member, device: Device, opened_by) -> FobEnrolment:
    reason = can_have_fob_reason(member)
    if reason:
        raise FobError(reason)
    if not device.enabled:
        raise FobError(f"{device} is disabled.")
    with transaction.atomic():
        Device.objects.select_for_update().filter(pk=device.pk).first()
        current = waiting_enrolment(device)
        if current is not None:
            raise FobError(
                f"{current.opened_by} is already waiting for a fob on {device.name} "
                f"(for {current.member}, {current.seconds_left} seconds left). Try again shortly or use another reader."
            )
        now = timezone.now()
        return FobEnrolment.objects.create(
            member=member,
            device=device,
            opened_by=opened_by,
            opened_at=now,
            expires_at=now + timedelta(seconds=SiteSettings.load().fob_enrolment_seconds),
        )


def capture(device: Device, *, card_number: int | None, tag_id: str = "") -> FobEnrolment | None:
    """A fob BBMS doesn't know was tapped on `device`. If a window is open there, capture it.

    Returns the enrolment that holds this card (just captured, or captured by an earlier tap during the same window,
    as today's door sends up to 13 lookups per tap), or None.
    """
    if card_number is None or not 0 < card_number <= MAX_CARD_NUMBER:
        return None
    if KeyFob.objects.filter(card_number=card_number).exists():
        return None  # a lost fob, say: not something to enrol again
    now = timezone.now()
    with transaction.atomic():
        enrolment = (
            FobEnrolment.objects.select_for_update()
            .filter(device=device, cancelled_at__isnull=True, confirmed_at__isnull=True, expires_at__gt=now)
            .select_related("member")
            .first()
        )
        if enrolment is None:
            return None
        if enrolment.captured_at is None:
            enrolment.captured_card_number = card_number
            enrolment.captured_tag_id = tag_id or ""
            enrolment.captured_at = now
            enrolment.save(update_fields=["captured_card_number", "captured_tag_id", "captured_at"])
            return enrolment
        # Only the first unknown fob is captured; a repeat of the same one is still "enrolled".
        return enrolment if enrolment.captured_card_number == card_number else None


def capture_v1_tag(device: Device, raw_tag: str) -> FobEnrolment | None:
    """From today's firmware: the vendor prefix is a guess, so only the card number is kept."""
    return capture(device, card_number=tags.card_number(raw_tag))


def capture_v2_card(device: Device, card) -> FobEnrolment | None:
    """From a v2 reader, which sends the card number as read, or the full tag."""
    value = tags.normalise(str(card))
    if len(value) == 12:
        return capture(device, card_number=tags.card_number(value), tag_id=value)
    if value.isascii() and value.isdigit() and len(value) <= 10:
        return capture(device, card_number=int(value))
    return None


def confirm_enrolment(enrolment: FobEnrolment, confirmed_by) -> KeyFob:
    with transaction.atomic():
        enrolment = FobEnrolment.objects.select_for_update().select_related("member").get(pk=enrolment.pk)
        if enrolment.state != FobEnrolment.State.CAPTURED:
            raise FobError(f"Nothing to confirm: {enrolment.get_state_display().lower()}.")
        fob = issue_fob(
            enrolment.member,
            card_number=enrolment.captured_card_number,
            tag_id=enrolment.captured_tag_id or None,
            issued_by=confirmed_by,
        )
        enrolment.fob = fob
        enrolment.confirmed_by = confirmed_by
        enrolment.confirmed_at = timezone.now()
        enrolment.save(update_fields=["fob", "confirmed_by", "confirmed_at"])
    return fob


def cancel_enrolment(enrolment: FobEnrolment):
    if enrolment.state in (FobEnrolment.State.WAITING, FobEnrolment.State.CAPTURED):
        enrolment.cancelled_at = timezone.now()
        enrolment.save(update_fields=["cancelled_at"])
