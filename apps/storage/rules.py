"""
Locker rules (spec 06 §6.2). Every change to a price, a locker's availability, a rental, a member's limit or a
member's box flag goes through a function here, so the rules live in one place and every change is logged in
LockerChange.
"""

from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

from apps.site_settings.models import SiteSettings

from .models import Locker, LockerChange, LockerRent, LockerRental, MemberLockerLimit

RENTED = "rented"
AVAILABLE = "available"
NO_PRICE = "no_price"  # nobody has set a default rent for its size and location, and it has no override
NOTICE_DAYS = 30  # a rise in a default rent reaches existing renters after this many days (06 §6.2)


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


def rental_rent_pence(rental, on: date | None = None) -> int | None:
    """What this member pays a month for a charge dated `on` (default today): their rental's override if set,
    otherwise the default rent they agreed to, or a new one once its notice is up (06 §6.2)."""
    if rental.rent_override_pence is not None:
        return rental.rent_override_pence
    on = on or timezone.localdate()
    if rental.new_rent_from is not None and on >= rental.new_rent_from:
        return rental.new_rent_pence
    if rental.rent_pence is not None:
        return rental.rent_pence
    return default_rent_pence(rental.locker)


def upcoming_rent(rental) -> tuple[int, date] | None:
    """A new rent the member has been told about that hasn't started yet: (pence, from)."""
    if rental.rent_override_pence is None and rental.new_rent_from and rental.new_rent_from > timezone.localdate():
        return rental.new_rent_pence, rental.new_rent_from
    return None


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
    rental = LockerRental.objects.create(
        locker=locker,
        member=member,
        started_by=by,
        rent_override_pence=locker.advertised_rent_pence,
        rent_override_reason=locker.advertised_rent_reason,
        rent_pence=default_rent_pence(locker),
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
    if pence is None:  # back to the default: today's, with nothing waiting
        rental.rent_pence, rental.new_rent_pence, rental.new_rent_from = default_rent_pence(rental.locker), None, None
    rental.save(
        update_fields=["rent_override_pence", "rent_override_reason", "rent_pence", "new_rent_pence", "new_rent_from"]
    )
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
    # Rent stops: charges not yet billed are cancelled. No refund for the month already paid (❓ 06 §6.2).
    from apps.billing.billing import cancel_rent_charges

    cancel_rent_charges(rental)
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


# --- members renting and giving up lockers themselves (06 §6.2) ---


def self_service_problem(member, locker=None) -> str | None:
    """Why this member can't rent (this locker) on the lockers page, or None if they can."""
    from apps.billing import billing

    if member.status not in (member.Status.ACTIVE, member.Status.HONORARY):
        return "Only current members can rent a locker."
    held = open_rentals(member).count()
    if held >= locker_limit(member):
        return f"You already have {held} locker{'s' if held != 1 else ''}, your limit."
    if locker is not None and (locker.retired or status(locker) != AVAILABLE):
        return f"{locker} isn't available any more."
    return billing.rent_payment_problem(member)


def first_rent(member, monthly_pence: int, today: date | None = None) -> tuple[int, date]:
    """The first payment for a locker rented today: the rent for the days until the member's next payment day, and
    that day (❓ 06 §6.2, the suggested rule). Renting on the payment day itself pays for a whole month.
    """
    from apps.billing.membership import add_month, payment_day_for

    today = today or timezone.localdate()
    day = member.payment_day or payment_day_for(today)
    this_month = today.replace(day=day)
    if this_month > today:
        # Last month's payment day (payment days are 1-28, so it exists in every month).
        month_before = this_month.replace(day=1) - timedelta(days=1)
        next_day, previous = this_month, month_before.replace(day=day)
    else:
        next_day, previous = add_month(this_month), this_month
    days, period = (next_day - today).days, (next_day - previous).days
    return (monthly_pence * days + period // 2) // period, next_day


def rent_locker(locker, member) -> tuple[LockerRental, int]:
    """A member rents an available locker themselves and pays the first rent straight away (06 §6.2). Returns the
    rental and what the first payment was. Raises LockerError, changing nothing, if they can't.
    """
    from apps.billing import billing
    from apps.members.models import Member

    today = timezone.localdate()
    with transaction.atomic():
        member = Member.objects.select_for_update().get(pk=member.pk)
        problem = self_service_problem(member, locker)
        if problem:
            raise LockerError(problem)
        rental = start_rental(locker, member, member)
        amount = billing.take_first_rent(rental, today)
    from apps.billing import emails
    from apps.notifications.notify import notify_role

    rent = rental_rent_pence(rental)
    emails.send(
        member,
        "storage/emails/locker_rented",
        locker=rental.locker,
        rent=pounds(rent),
        first=pounds(amount),
        payment_day=member.payment_day,
    )
    emails.tell_storage_team(
        "storage/emails/storage_locker_rented", member=member, locker=rental.locker, rent=pounds(rent)
    )
    notify_role(
        emails.STORAGE_TEAM,
        message=f"{member} rented {rental.locker}.",
        kind="locker rented",
        url="/admin/storage/locker/",
    )
    return rental, amount


def give_up(rental, member) -> LockerRental:
    """A member gives up their own locker. Rent stops from today; there's no refund (❓ 06 §6.2)."""
    if rental.member_id != member.pk:
        raise LockerError("That isn't your locker.")
    if not rental.is_open:
        raise LockerError("You've already given that locker up.")
    rental = end_rental(rental, member, "Given up by the member")
    from apps.billing import emails
    from apps.notifications.notify import notify_role

    emails.tell_storage_team("storage/emails/storage_locker_given_up", member=member, locker=rental.locker)
    notify_role(
        emails.STORAGE_TEAM,
        message=f"{member} gave up {rental.locker}: check it's empty.",
        kind="locker given up",
        url="/admin/storage/locker/",
    )
    return rental


# --- the "box paid" flag (06 §6.2): boxes have no inventory, only whether the member has paid for one ---


@transaction.atomic
def give_box_flag(member, by, *, payment=None, reason: str = "") -> bool:
    """Record that a member has paid for a box: from a payment, or ticked by the storage team for a fee paid outside
    BBMS (then a reason is needed). Returns whether anything changed; a member who has it already keeps their date.
    """
    member = type(member).objects.select_for_update().get(pk=member.pk)
    if member.box_paid:
        return False
    if payment is None and not reason:
        raise LockerError("Say how the box fee was paid, e.g. cash or before BBMS recorded box fees.")
    member.box_paid_at = (payment.paid_at if payment is not None else None) or timezone.now()
    member.box_payment = payment
    member.save(update_fields=["box_paid_at", "box_payment"])
    _log(LockerChange.What.BOX_FLAG, str(member), "no", "yes", reason or "Box fee paid", by, member=member)
    return True


@transaction.atomic
def clear_box_flag(member, reason: str, by) -> bool:
    """Take the flag away (e.g. set in error). The reason is logged."""
    member = type(member).objects.select_for_update().get(pk=member.pk)
    if not member.box_paid:
        return False
    if not reason:
        raise LockerError("Give a reason for clearing the box flag.")
    member.box_paid_at = None
    member.box_payment = None
    member.save(update_fields=["box_paid_at", "box_payment"])
    _log(LockerChange.What.BOX_FLAG, str(member), "yes", "no", reason, by, member=member)
    return True


def box_payment_failed(payments):
    """A Direct Debit box fee that wasn't paid after all (a pending payment already counts): the flag it set goes."""
    from apps.members.models import Member

    for member in Member.objects.filter(box_payment__in=payments):
        clear_box_flag(member, "The box fee payment failed", None)


def default_rent_changed(rent: LockerRent, old_pence: int | None, by, reason: str = "") -> dict:
    """After an admin changes a default rent: log it, and move its renters without an override to it (06 §6.2).

    A lower rent applies at once. A higher one applies from the first charge at least 30 days after the renter is
    emailed about it (❓ the suggested notice); until then they pay what they did. Returns how many renters were
    {"lowered": n, "raised": n}.
    """
    from apps.billing import emails

    description = f"{rent.size} lockers in {rent.location}"
    _log(LockerChange.What.DEFAULT_RENT, description, pounds(old_pence), pounds(rent.monthly_rent_pence), reason, by)
    today = timezone.localdate()
    new = rent.monthly_rent_pence
    counts = {"lowered": 0, "raised": 0}
    renters = (
        LockerRental.objects.select_for_update()
        .filter(
            locker__location=rent.location_id,
            locker__size=rent.size_id,
            ended_at__isnull=True,
            rent_override_pence__isnull=True,
        )
        .select_related("locker", "member")
    )
    with transaction.atomic():
        for rental in renters:
            paying = rental_rent_pence(rental, today)
            if paying is None:
                paying = old_pence
            if new == paying:
                rental.rent_pence, rental.new_rent_pence, rental.new_rent_from = new, None, None
            elif new < paying:
                rental.rent_pence, rental.new_rent_pence, rental.new_rent_from = new, None, None
                counts["lowered"] += 1
            else:
                rental.rent_pence = paying
                rental.new_rent_pence, rental.new_rent_from = new, today + timedelta(days=NOTICE_DAYS)
                counts["raised"] += 1
            rental.save(update_fields=["rent_pence", "new_rent_pence", "new_rent_from"])
            if new != paying:
                emails.send(
                    rental.member,
                    "storage/emails/rent_changing",
                    locker=rental.locker,
                    old=pounds(paying),
                    new=pounds(new),
                    starts=rental.new_rent_from,
                    lower=new < paying,
                )
                _log(
                    LockerChange.What.RENTAL_RENT,
                    f"{rental.locker} ({rental.member})",
                    pounds(paying),
                    pounds(new) + (f" from {rental.new_rent_from:%d %b %Y}" if rental.new_rent_from else ""),
                    "The default rent changed",
                    by,
                    locker=rental.locker,
                    rental=rental,
                    member=rental.member,
                )
    return counts


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
