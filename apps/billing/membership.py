"""
A member's status as it follows their payments (spec 03 §3.2-3.8).

Every status change caused by billing goes through these functions, so the side effects (emails, charges cancelled,
lockers released) always happen together.
"""

import calendar
import logging
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

from apps.members.models import Member
from apps.site_settings.models import SiteSettings

from . import emails, gocardless
from .models import Charge

logger = logging.getLogger(__name__)


def add_month(day: date) -> date:
    """The same day next month, or the month's last day if it's shorter (31 Jan → 28/29 Feb)."""
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def payment_day_for(today: date) -> int:
    """A new payment day: today's day of the month, or the 1st after the 28th (03 §3.1)."""
    return today.day if today.day <= 28 else 1


def paid_until(member) -> date | None:
    """The latest paid subscription charge's date + 1 month (03 §3.6)."""
    latest = (
        member.charges.filter(kind=Charge.Kind.SUBSCRIPTION, status=Charge.Status.PAID)
        .order_by("-charge_date")
        .values_list("charge_date", flat=True)
        .first()
    )
    return add_month(latest) if latest else None


def extend_expiry(member, until: date):
    """Move "paid until" later (never earlier)."""
    if member.subscription_expires is None or until > member.subscription_expires:
        member.subscription_expires = until
        member.save(update_fields=["subscription_expires"])


def _set_status(member, status, **extra):
    was = member.status
    member.status = status
    for name, value in extra.items():
        setattr(member, name, value)
    member.save(update_fields=["status", *extra])
    logger.info("Member %s: %s → %s", member.pk, was, status)
    from apps.discord.linking import sync_soon

    sync_soon(member)  # their member / former-member role on Discord (06 §6.14)
    return was


def _cancel_open_charges(member, statuses=(Charge.Status.PENDING, Charge.Status.DUE)):
    return member.charges.filter(status__in=statuses).update(status=Charge.Status.CANCELLED)


# --- joining ---


@transaction.atomic
def activate(member, today: date | None = None):
    """After a payment method is set up ("ensure membership active", 03 §3.4).

    The member becomes active (honorary members stay honorary). If nothing is owed and nothing paid covers today,
    a charge dated today is created as due: Direct Debit payers are billed for it at once; credit payers at the
    next nightly run.
    """
    from . import billing

    today = today or timezone.localdate()
    member = Member.objects.select_for_update().get(pk=member.pk)
    was = member.status
    if member.status != Member.Status.HONORARY:
        _set_status(member, Member.Status.ACTIVE, suspended_at=None)
    if was == Member.Status.SETTING_UP:
        emails.send(member, "welcome")

    if member.status == Member.Status.HONORARY:
        return member
    owes = member.charges.filter(kind=Charge.Kind.SUBSCRIPTION, status__in=Charge.OPEN).exists()
    if owes or (member.subscription_expires and member.subscription_expires >= today):
        return member
    charge_date = today
    if today.day > 28 and member.payment_day == 1:
        charge_date = add_month(today).replace(day=1)
    charge, _ = Charge.objects.get_or_create(
        member=member, kind=Charge.Kind.SUBSCRIPTION, charge_date=charge_date, defaults={"status": Charge.Status.DUE}
    )
    if charge.status == Charge.Status.PENDING:
        charge.status = Charge.Status.DUE
        charge.save(update_fields=["status"])
    if member.payment_method == Member.PaymentMethod.DIRECT_DEBIT:
        try:
            billing.bill_member(member, today)
        except gocardless.GoCardlessError as error:
            # The set-up still counts; the charge stays due and tonight's billing run tries again.
            logger.warning("Member %s: first Direct Debit not taken yet: %s", member.pk, error)
    return member


@transaction.atomic
def rejoin(member):
    """A member who has left starts again (03 §3.8): back to setting up a payment."""
    member = Member.objects.select_for_update().get(pk=member.pk)
    if member.status != Member.Status.LEFT:
        return member
    _set_status(member, Member.Status.SETTING_UP, payment_method="", payment_day=None, suspended_at=None)
    return member


# --- payments arriving ---


def charge_processing(charge: Charge):
    """A Direct Debit is on its way: membership is extended to the charge date + 1 month (03 §3.3)."""
    charge.status = Charge.Status.PROCESSING
    charge.save(update_fields=["status"])
    if charge.kind == Charge.Kind.SUBSCRIPTION:
        extend_expiry(charge.member, add_month(charge.charge_date))


def charge_paid(charge: Charge, paid_on: date):
    """A charge is paid (03 §3.3): payment date + 1 month, and a suspended member is active again."""
    charge.status = Charge.Status.PAID
    charge.paid_on = paid_on
    charge.retry_on = None
    charge.save(update_fields=["status", "paid_on", "retry_on"])
    if charge.kind != Charge.Kind.SUBSCRIPTION:
        return
    member = charge.member
    extend_expiry(member, add_month(paid_on))
    if member.status == Member.Status.SUSPENDED:
        _set_status(member, Member.Status.ACTIVE, suspended_at=None)


def charge_failed(charge: Charge, today: date):
    """A Direct Debit has finally failed (03 §3.6): the member can pay it another way; expiry goes back."""
    charge.status = Charge.Status.FAILED
    charge.retry_on = None
    charge.save(update_fields=["status", "retry_on"])
    if charge.kind == Charge.Kind.SUBSCRIPTION:
        member = charge.member
        member.subscription_expires = paid_until(member) or today
        member.save(update_fields=["subscription_expires"])
    elif charge.rental_id:
        emails.tell_storage_team(
            "storage_rent_failed",
            member=charge.member,
            locker=charge.rental.locker,
            amount=f"{(charge.amount_pence or 0) / 100:.2f}",
        )


# --- leaving ---


@transaction.atomic
def start_leaving(member, *, cancel_mandate=True, paypal_cancelled=False):
    """The member leaves, or their Direct Debit or PayPal subscription is cancelled (03 §3.8): no more charges;
    access continues until their paid-until date, then the morning check marks them as left.

    BBMS can't cancel a PayPal subscription, so a PayPal payer who leaves is asked to cancel it at PayPal, unless
    PayPal has just said it's cancelled (`paypal_cancelled`).
    """
    member = Member.objects.select_for_update().get(pk=member.pk)
    if member.status in (Member.Status.LEFT, Member.Status.LEAVING):
        return member
    if cancel_mandate and member.mandate_id:
        gocardless.client().cancel_mandate(member.mandate_id)
    cancel_paypal = member.payment_method == Member.PaymentMethod.PAYPAL and not paypal_cancelled
    _cancel_open_charges(member)
    _set_status(member, Member.Status.LEAVING, payment_method="", mandate_id="")
    emails.send(member, "leaving", cancel_paypal=cancel_paypal)
    return member


@transaction.atomic
def mandate_gone(member):
    """GoCardless says the mandate was cancelled (by the member's bank, say), failed or expired."""
    member = Member.objects.select_for_update().get(pk=member.pk)
    if member.payment_method == Member.PaymentMethod.DIRECT_DEBIT and member.status not in (
        Member.Status.LEFT,
        Member.Status.SETTING_UP,
    ):
        member.mandate_id = ""
        member.save(update_fields=["mandate_id"])
        return start_leaving(member, cancel_mandate=False)
    # A credit payer's backup, or someone who has already left: just forget it.
    member.mandate_id = ""
    member.save(update_fields=["mandate_id"])
    return member


def suspend(member, now=None):
    _set_status(member, Member.Status.SUSPENDED, suspended_at=now or timezone.now())
    emails.send(member, "suspended", days=SiteSettings.load().suspended_to_left_days)


@transaction.atomic
def become_left(member, today: date | None = None):
    """Membership ends (03 §3.6): lockers released, charges cancelled, payment day cleared, emails sent."""
    from apps.storage import rules as storage

    member = Member.objects.select_for_update().get(pk=member.pk)
    if member.status == Member.Status.LEFT:
        return member
    lockers = []
    for rental in storage.open_rentals(member).select_related("locker"):
        storage.end_rental(rental, None, "Membership ended")
        lockers.append(str(rental.locker))
    _cancel_open_charges(member)
    _set_status(member, Member.Status.LEFT, payment_day=None, suspended_at=None)
    box = member.box_paid
    emails.send(member, "left", lockers=lockers, box=box)
    if lockers or box:
        emails.tell_storage_team("storage_member_left", member=member, lockers=lockers, box=box)
    return member


# --- 06:00: check memberships ---


def check_memberships(today: date | None = None, now=None) -> dict:
    """The morning check (03 §3.6), in this order:

    1. suspended members: re-activated if a paid charge now covers them; marked as left once suspended for the
       number of days in Settings (30);
    2. leaving members past their paid-until date: left;
    3. active members past their paid-until date and grace period: extended if a paid charge covers a later date,
       otherwise suspended.

    Honorary members are never checked. (Legacy's "payment-warning" pass isn't needed: no member has that status.)
    """
    now = now or timezone.now()
    today = today or timezone.localdate(now)
    config = SiteSettings.load()
    results = {"re-activated": 0, "left": 0, "extended": 0, "suspended": 0}

    def cutoff(member):
        return today - timedelta(days=config.grace_days(member.payment_method))

    for member in Member.objects.filter(status=Member.Status.SUSPENDED):
        covered = paid_until(member)
        if covered and covered >= cutoff(member):
            with transaction.atomic():
                extend_expiry(member, covered)
                _set_status(member, Member.Status.ACTIVE, suspended_at=None)
            results["re-activated"] += 1
        elif member.suspended_at is None:
            # Suspended before BBMS recorded when (imported, say): the 30 days start now.
            member.suspended_at = now
            member.save(update_fields=["suspended_at"])
        elif member.suspended_at <= now - timedelta(days=config.suspended_to_left_days):
            become_left(member, today)
            results["left"] += 1

    for member in Member.objects.filter(status=Member.Status.LEAVING):
        if member.subscription_expires is None or member.subscription_expires < today:
            become_left(member, today)
            results["left"] += 1

    for member in Member.objects.filter(status=Member.Status.ACTIVE):
        expires = member.subscription_expires
        if expires is not None and expires >= cutoff(member):
            continue
        covered = paid_until(member)
        if covered and covered >= cutoff(member) and (expires is None or covered > expires):
            extend_expiry(member, covered)
            results["extended"] += 1
        else:
            with transaction.atomic():
                suspend(member, now)
            results["suspended"] += 1
    return results
