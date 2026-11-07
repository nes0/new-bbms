"""
Locker rules (spec 06 §6.2). Every change to a price, a locker's availability, a rental or a member's limit goes
through a function here, so the rules live in one place and every change is logged in LockerChange.
"""

from django.db import transaction
from django.utils import timezone

from apps.site_settings.models import SiteSettings

from .models import Locker, LockerChange, LockerRent, LockerRental, MemberLockerLimit

RENTED = "rented"
AVAILABLE = "available"
NO_PRICE = "no_price"  # nobody has set a default rent for its size and location, and it has no override


class LockerError(Exception):
    """A change the rules don't allow; the message says why, for the person who tried it."""


def pounds(pence) -> str:
    return "—" if pence is None else f"£{pence / 100:.2f}"


# --- prices ---


def default_rent_pence(locker) -> int | None:
    rent = LockerRent.objects.filter(location=locker.location_id, size=locker.size_id).first()
    return rent.monthly_rent_pence if rent else None


def advertised_rent_pence(locker) -> int | None:
    """What an empty locker costs a month: its override if set, otherwise its size and location's default."""
    if locker.advertised_rent_pence is not None:
        return locker.advertised_rent_pence
    return default_rent_pence(locker)


def rental_rent_pence(rental) -> int | None:
    """What this member pays a month: their rental's override if set, otherwise the current default."""
    # ❓ 06 §6.2: when a default rent changes, existing renters pay it after notice (suggested 30 days). Until billing
    # exists this is simply the current default. TODO(subscriptions app): apply the notice period.
    if rental.rent_override_pence is not None:
        return rental.rent_override_pence
    return default_rent_pence(rental.locker)


# --- status ---


def open_rental(locker):
    return locker.rentals.filter(ended_at__isnull=True).select_related("member").first()


def status(locker) -> str:
    """rented, reserved, out_of_use, no_price or available."""
    if open_rental(locker) is not None:
        return RENTED
    if locker.hold:
        return locker.hold
    if advertised_rent_pence(locker) is None:
        return NO_PRICE
    return AVAILABLE


def available_lockers():
    """Lockers a member could rent now, with their prices, for the members' page."""
    rented = LockerRental.objects.filter(ended_at__isnull=True).values("locker")
    lockers = (
        Locker.objects.filter(retired=False, hold=Locker.Hold.NONE)
        .exclude(pk__in=rented)
        .select_related("location", "size")
    )
    rents = {(r.location_id, r.size_id): r.monthly_rent_pence for r in LockerRent.objects.all()}
    result = []
    for locker in lockers:
        price = locker.advertised_rent_pence
        if price is None:
            price = rents.get((locker.location_id, locker.size_id))
        if price is not None:
            result.append((locker, price))
    return result


# --- members' limits ---


def locker_limit(member) -> int:
    """How many lockers this member may rent: their own limit if a trustee set one, otherwise the setting."""
    override = MemberLockerLimit.objects.filter(member=member).first()
    return override.limit if override else SiteSettings.load().lockers_per_member


def open_rentals(member):
    return LockerRental.objects.filter(member=member, ended_at__isnull=True).select_related("locker__location")


@transaction.atomic
def set_member_limit(member, limit: int | None, reason: str, by):
    """Give a member their own limit (None goes back to the usual one)."""
    if not reason:
        raise LockerError("Give a reason.")
    old = MemberLockerLimit.objects.filter(member=member).first()
    old_value = str(old.limit) if old else f"usual ({SiteSettings.load().lockers_per_member})"
    if limit is None:
        MemberLockerLimit.objects.filter(member=member).delete()
        new_value = f"usual ({SiteSettings.load().lockers_per_member})"
    else:
        MemberLockerLimit.objects.update_or_create(
            member=member, defaults={"limit": limit, "reason": reason, "set_by": by, "set_at": timezone.now()}
        )
        new_value = str(limit)
    _log(LockerChange.What.MEMBER_LIMIT, str(member), old_value, new_value, reason, by, member=member)


# --- changing a locker's price or availability (only while it's empty) ---


def _lock(locker) -> Locker:
    return Locker.objects.select_for_update().get(pk=locker.pk)


@transaction.atomic
def set_advertised_rent(locker, pence: int | None, reason: str, by):
    """Override the price shown for an empty locker (None goes back to the default)."""
    locker = _lock(locker)
    if open_rental(locker) is not None:
        raise LockerError("It's rented: change the rent on the rental instead.")
    if pence is not None and not reason:
        raise LockerError("Give a reason for the override.")
    old = pounds(locker.advertised_rent_pence) if locker.advertised_rent_pence is not None else "default"
    locker.advertised_rent_pence = pence
    locker.advertised_rent_reason = reason if pence is not None else ""
    locker.save(update_fields=["advertised_rent_pence", "advertised_rent_reason"])
    new = pounds(pence) if pence is not None else "default"
    _log(LockerChange.What.ADVERTISED_RENT, str(locker), old, new, reason, by, locker=locker)
    return locker


@transaction.atomic
def set_availability(locker, hold: str, note: str, by):
    """Mark an empty locker available, reserved for trustees or out of use."""
    locker = _lock(locker)
    if hold not in Locker.Hold.values:
        raise LockerError("Unknown availability.")
    if open_rental(locker) is not None:
        raise LockerError("It's rented: end the rental first.")
    if hold and not note:
        raise LockerError("Say what it's reserved for, or what's wrong with it.")
    old = Locker.Hold(locker.hold).label
    locker.hold = hold
    locker.hold_note = note if hold else ""
    locker.save(update_fields=["hold", "hold_note"])
    _log(LockerChange.What.AVAILABILITY, str(locker), old, Locker.Hold(hold).label, note, by, locker=locker)
    return locker


# --- rentals ---


@transaction.atomic
def start_rental(locker, member, by) -> LockerRental:
    """Rent an available locker to a member. An advertised price moves onto the rental (06 §6.2)."""
    locker = _lock(locker)
    state = status(locker)
    if locker.retired or state != AVAILABLE:
        raise LockerError(f"{locker} isn't available ({state.replace('_', ' ')}).")
    if not member.has_space_access:
        raise LockerError(f"{member} isn't a current member.")
    held = open_rentals(member).count()
    if held >= locker_limit(member):
        raise LockerError(f"{member} already has {held} locker(s), their limit.")
    # TODO(subscriptions app): self-service renting needs a Direct Debit or credit to pay from, and takes the first
    # (pro-rata) payment straight away (06 §6.2).
    rental = LockerRental.objects.create(
        locker=locker,
        member=member,
        started_by=by,
        rent_override_pence=locker.advertised_rent_pence,
        rent_override_reason=locker.advertised_rent_reason,
    )
    if locker.advertised_rent_pence is not None:
        locker.advertised_rent_pence = None
        locker.advertised_rent_reason = ""
        locker.save(update_fields=["advertised_rent_pence", "advertised_rent_reason"])
    _log(
        LockerChange.What.RENTED,
        str(locker),
        "",
        f"{member} at {pounds(rental_rent_pence(rental))} a month",
        "",
        by,
        locker=locker,
        rental=rental,
        member=member,
    )
    return rental


@transaction.atomic
def set_rental_rent(rental, pence: int | None, reason: str, by):
    """Change what one member pays for their locker (None goes back to the default)."""
    rental = LockerRental.objects.select_for_update().get(pk=rental.pk)
    if not rental.is_open:
        raise LockerError("That rental has ended.")
    if not reason:
        raise LockerError("Give a reason.")
    old = pounds(rental.rent_override_pence) if rental.rent_override_pence is not None else "default"
    rental.rent_override_pence = pence
    rental.rent_override_reason = reason if pence is not None else ""
    rental.save(update_fields=["rent_override_pence", "rent_override_reason"])
    new = pounds(pence) if pence is not None else "default"
    _log(
        LockerChange.What.RENTAL_RENT,
        f"{rental.locker} ({rental.member})",
        old,
        new,
        reason,
        by,
        locker=rental.locker,
        rental=rental,
        member=rental.member,
    )
    return rental


@transaction.atomic
def end_rental(rental, by, reason: str = "") -> LockerRental:
    """Give a locker up. Rent stops from today; the locker becomes available at its default price."""
    rental = LockerRental.objects.select_for_update().get(pk=rental.pk)
    if not rental.is_open:
        return rental  # already ended: nothing to do
    rental.ended_at = timezone.now()
    rental.ended_by = by
    rental.end_reason = reason
    rental.save(update_fields=["ended_at", "ended_by", "end_reason"])
    # TODO(subscriptions app): cancel pending rent charges; no refund for the month already paid (❓ 06 §6.2).
    _log(
        LockerChange.What.ENDED,
        str(rental.locker),
        str(rental.member),
        "",
        reason,
        by,
        locker=rental.locker,
        rental=rental,
        member=rental.member,
    )
    return rental


def log_default_rent_change(rent: LockerRent, old_pence: int | None, by, reason: str = ""):
    description = f"{rent.size} lockers in {rent.location}"
    _log(LockerChange.What.DEFAULT_RENT, description, pounds(old_pence), pounds(rent.monthly_rent_pence), reason, by)


def _log(what, description, old, new, reason, by, *, locker=None, rental=None, member=None):
    LockerChange.objects.create(
        what=what,
        description=description[:255],
        old_value=old[:255],
        new_value=new[:255],
        reason=reason[:255],
        changed_by=by,
        locker=locker,
        rental=rental,
        member=member,
    )
