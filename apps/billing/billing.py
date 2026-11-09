"""
Creating and billing charges (spec 03 §3.3, §3.6; 06 §6.2).

The nightly jobs call `create_charges`, `bill_members` and `retry_direct_debits`; the account page and the admin call
`pay_charge_from_credit` and `pay_subscription_manually`. All of them are safe to run twice.
"""

import logging
from collections import defaultdict
from datetime import date, timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.members.models import Member
from apps.payments import services
from apps.payments.credit import credit_pence
from apps.payments.models import Payment
from apps.site_settings.models import SiteSettings

from . import emails, gocardless, membership
from .models import Charge

logger = logging.getLogger(__name__)

DAYS_AHEAD = 7  # charges are created up to a week before their date (03 §3.6)


def _pounds(pence):
    return f"{pence / 100:.2f}"


def _what(charges):
    kinds = {c.kind for c in charges}
    if kinds == {Charge.Kind.SUBSCRIPTION}:
        return "subscription"
    if kinds == {Charge.Kind.LOCKER_RENT}:
        return "locker rent"
    return "subscription and locker rent"


# --- 01:00: create charges ---


def create_charges(today: date) -> dict:
    """Pending subscription and locker rent charges for each date from today to a week ahead (03 §3.6).

    Subscriptions: every active (not honorary) member whose payment day is that date's day.
    Locker rent: every open rental whose member is active or honorary (honorary members pay rent, 06 §6.2) and whose
    payment day matches, for rentals started by then and with a rent above £0.
    """
    from apps.storage import rules as storage
    from apps.storage.models import LockerRental

    made = {"subscription": 0, "locker-rent": 0}
    for offset in range(DAYS_AHEAD + 1):
        day = today + timedelta(days=offset)
        for member in Member.objects.filter(status=Member.Status.ACTIVE, payment_day=day.day):
            _, created = Charge.objects.get_or_create(
                member=member,
                kind=Charge.Kind.SUBSCRIPTION,
                charge_date=day,
                defaults={"status": Charge.Status.PENDING},
            )
            made["subscription"] += created
        rentals = LockerRental.objects.filter(
            ended_at__isnull=True,
            started_at__date__lte=day,
            member__payment_day=day.day,
            member__status__in=(Member.Status.ACTIVE, Member.Status.HONORARY),
        ).select_related("member", "locker")
        for rental in rentals:
            if not storage.rental_rent_pence(rental):
                continue  # no rent (a £0 override, or no default rent set)
            _, created = Charge.objects.get_or_create(
                member=rental.member,
                kind=Charge.Kind.LOCKER_RENT,
                rental=rental,
                charge_date=day,
                defaults={"status": Charge.Status.PENDING},
            )
            made["locker-rent"] += created
    return made


# --- 01:30: bill members ---


def amount_for(charge: Charge) -> int | None:
    """What a charge is for, worked out when it's billed: the member's current monthly amount, or the rental's rent."""
    from apps.storage import rules as storage

    if charge.kind == Charge.Kind.SUBSCRIPTION:
        return charge.member.monthly_subscription_pence
    if charge.first_rent and charge.amount_pence:
        return charge.amount_pence  # pro rata, worked out when the locker was rented
    return storage.rental_rent_pence(charge.rental, charge.charge_date)


def make_due(today: date) -> int:
    """Pending charges dated today or earlier become due."""
    return Charge.objects.filter(status=Charge.Status.PENDING, charge_date__lte=today).update(status=Charge.Status.DUE)


def bill_members(today: date) -> dict:
    """Bill every due charge (03 §3.6). Direct Debit payers get one payment for everything due on their payment day."""
    make_due(today)
    results = defaultdict(int)
    member_ids = Charge.objects.filter(status=Charge.Status.DUE).values_list("member_id", flat=True).distinct()
    for member in Member.objects.filter(pk__in=set(member_ids)).order_by("pk"):
        try:
            outcome = bill_member(member, today)
        except gocardless.GoCardlessError as error:
            logger.error("Billing member %s failed: %s", member.pk, error)
            outcome = "error"
        results[outcome] += 1
    return dict(results)


def _due_charges(member):
    return list(
        # of=("self",): Postgres won't lock the optional (outer-joined) rental row.
        Charge.objects.select_for_update(of=("self",))
        .filter(member=member, status=Charge.Status.DUE)
        .select_related("rental__locker", "member")
        .order_by("charge_date", "kind", "pk")
    )


@transaction.atomic
def bill_member(member, today: date) -> str:
    """Bill one member's due charges. Returns what happened (for the job's summary)."""
    member = Member.objects.select_for_update().get(pk=member.pk)
    charges = _due_charges(member)
    if not charges:
        return "nothing due"
    for charge in charges:
        amount = amount_for(charge)
        if not amount:
            logger.warning("Charge %s (member %s) has no amount to bill; left due", charge.pk, member.pk)
            continue
        if charge.amount_pence != amount:
            charge.amount_pence = amount
            charge.save(update_fields=["amount_pence"])
    charges = [c for c in charges if c.amount_pence]
    if not charges:
        return "no amount"

    method = member.payment_method
    if method == Member.PaymentMethod.CREDIT:
        return _bill_from_credit(member, charges, today)
    if method == Member.PaymentMethod.DIRECT_DEBIT:
        if not member.mandate_id:
            logger.error("Member %s pays by Direct Debit but has no mandate", member.pk)
            return "no mandate"
        _bill_by_direct_debit(member, charges)
        return "direct debit"
    if method == Member.PaymentMethod.PAYPAL:
        return _bill_paypal_payer(member, charges, today)
    # Standing orders are matched by the statement import (04 §4.6); cash is recorded by an admin.
    return "paid outside BBMS"


def _bill_from_credit(member, charges, today) -> str:
    unpaid = []
    for charge in charges:
        try:
            services.pay_from_credit(member, charge.reason, charge.amount_pence, charge=charge, reference=_ref(charge))
        except services.InsufficientCredit:
            unpaid.append(charge)
            continue
        membership.charge_paid(charge, today)
    if not unpaid:
        return "paid from credit"
    total = sum(c.amount_pence for c in unpaid)
    credit = _pounds(credit_pence(member))
    if member.mandate_id:
        # Direct Debit is their backup (03 §3.7).
        _bill_by_direct_debit(member, unpaid)
        emails.send(member, "trying_direct_debit", what=_what(unpaid), amount=_pounds(total), credit=credit)
        return "credit too low: direct debit"
    untold = [c for c in unpaid if c.member_told_at is None]
    if untold:
        emails.send(member, "credit_too_low", what=_what(untold), amount=_pounds(total), credit=credit)
        Charge.objects.filter(pk__in=[c.pk for c in untold]).update(member_told_at=timezone.now())
    return "credit too low"


def _bill_paypal_payer(member, charges, today) -> str:
    """PayPal pays by itself (04 §4.4). A payment that came before its charge went to credit, so a due charge is
    taken from credit if there's enough; otherwise it waits for PayPal, without emailing the member.
    """
    waiting = 0
    for charge in charges:
        try:
            services.pay_from_credit(member, charge.reason, charge.amount_pence, charge=charge, reference=_ref(charge))
        except services.InsufficientCredit:
            waiting += 1
            continue
        membership.charge_paid(charge, today)
    return "waiting for paypal" if waiting else "paid from credit"


def _ref(charge):
    return f"charge {charge.pk}" if charge.rental_id is None else f"charge {charge.pk}: {charge.rental.locker}"


def _bill_by_direct_debit(member, charges):
    """One GoCardless payment for all these charges; one pending ledger row per charge."""
    total = sum(c.amount_pence for c in charges)
    ids = ",".join(str(c.pk) for c in charges)
    payment = gocardless.client().create_payment(
        member.mandate_id,
        total,
        f"{SiteSettings.load().space_name} {_what(charges)}",
        # The same charges always give the same key, so a re-run after a crash can't take the money twice.
        idempotency_key=f"bbms-charges-{ids}",
        metadata={"member": str(member.pk), "charges": ids[:500]},
    )
    for charge in charges:
        services.record_payment(
            member,
            charge.reason,
            Payment.Source.GOCARDLESS,
            charge.amount_pence,
            status=Payment.Status.PENDING,
            source_id=payment.id,
            charge=charge,
            reference=_ref(charge),
        )
        membership.charge_processing(charge)
    logger.info("Member %s: Direct Debit %s for charges %s", member.pk, payment.id, ids)
    return payment


# --- failed Direct Debits (03 §3.6) ---


def retry_direct_debits(today: date) -> int:
    """BBMS's retries: ask GoCardless to try again the payments whose retry day has come."""
    count = 0
    charges = Charge.objects.filter(status=Charge.Status.PROCESSING, retry_on__lte=today)
    payment_ids = (
        Payment.objects.filter(charge__in=charges, source=Payment.Source.GOCARDLESS, status=Payment.Status.FAILED)
        .values_list("source_id", flat=True)
        .distinct()
    )
    for payment_id in payment_ids:
        try:
            with transaction.atomic():
                rows = locked_rows(payment_id)
                gocardless.client().retry_payment(payment_id)
                rows.update(status=Payment.Status.PENDING)
                for charge in Charge.objects.filter(payments__in=rows).distinct():
                    charge.retries += 1
                    charge.retry_on = None
                    charge.save(update_fields=["retries", "retry_on"])
            count += 1
        except gocardless.GoCardlessError as error:
            logger.error("Retrying Direct Debit %s failed: %s", payment_id, error)
    return count


def locked_rows(payment_id):
    """The ledger rows for one GoCardless payment, locked until the transaction ends.

    Returned as a plain query on their ids, so it can be used inside other queries (Postgres doesn't allow a locking
    query as a subquery).
    """
    query = Payment.objects.filter(source=Payment.Source.GOCARDLESS, source_id=payment_id)
    ids = list(query.select_for_update().values_list("pk", flat=True))
    return Payment.objects.filter(pk__in=ids)


# --- paying a charge another way ---


def charge_amount(charge: Charge) -> int | None:
    """What a member pays for a charge now: its billed amount, or what it would be billed."""
    return charge.amount_pence or amount_for(charge)


@transaction.atomic
def pay_charge(charge: Charge, source, today: date, *, amount_pence=None, **fields) -> Payment:
    """The member pays a due or failed charge themselves: from credit (raises InsufficientCredit) or by card.

    `amount_pence` is what a card payment actually charged.
    """
    charge = Charge.objects.select_for_update(of=("self",)).select_related("member", "rental__locker").get(pk=charge.pk)
    if charge.status not in Charge.UNPAID:
        raise ValueError("That charge isn't waiting to be paid.")
    amount = amount_pence or charge_amount(charge)
    if not amount:
        raise ValueError("That charge has no amount.")
    if charge.amount_pence != amount:
        charge.amount_pence = amount
        charge.save(update_fields=["amount_pence"])
    payment = services.pay(
        charge.member, charge.reason, source, amount, charge=charge, reference=_ref(charge), **fields
    )
    membership.charge_paid(charge, today)
    return payment


def pay_charge_from_credit(charge: Charge, today: date) -> Payment:
    """The member pays a due or failed charge from their credit (account page). Raises InsufficientCredit."""
    return pay_charge(charge, Payment.Source.BALANCE, today)


@transaction.atomic
def pay_subscription_manually(member, source, today: date, *, created_by=None, note="") -> Payment:
    """An admin records a subscription paid outside BBMS (cash, say; 04 §4.5): it pays the oldest unpaid charge, or a
    new one dated today, so membership is extended to a month from today.
    """
    member = Member.objects.select_for_update().get(pk=member.pk)
    amount = member.monthly_subscription_pence
    if not amount:
        raise ValueError(f"{member} has no monthly subscription amount.")
    charge = (
        member.charges.select_for_update()
        .filter(kind=Charge.Kind.SUBSCRIPTION)
        .filter(Q(status__in=Charge.UNPAID) | Q(status=Charge.Status.PENDING, charge_date__lte=today))
        .order_by("charge_date")
        .first()
    )
    if charge is None:
        charge, _ = Charge.objects.get_or_create(
            member=member, kind=Charge.Kind.SUBSCRIPTION, charge_date=today, defaults={"status": Charge.Status.DUE}
        )
        if charge.status not in (*Charge.UNPAID, Charge.Status.PENDING):
            raise ValueError("Today's subscription is already being paid.")
    charge.amount_pence = amount
    charge.save(update_fields=["amount_pence"])
    payment = services.pay(
        member,
        Payment.Reason.SUBSCRIPTION,
        source,
        amount,
        charge=charge,
        reference=_ref(charge),
        created_by=created_by,
        note=note,
    )
    membership.charge_paid(charge, today)
    return payment


def cancel_rent_charges(rental):
    """A locker given up: its rent charges that haven't been billed yet are cancelled (no refund for paid ones)."""
    return Charge.objects.filter(rental=rental, status__in=(Charge.Status.PENDING, Charge.Status.DUE)).update(
        status=Charge.Status.CANCELLED
    )


# --- the first rent for a locker a member rents themselves (06 §6.2) ---

MINIMUM_FIRST_RENT_PENCE = 100  # GoCardless can't take less than £1; a smaller first rent isn't charged


def rent_payment_problem(member) -> str | None:
    """Why this member can't pay locker rent monthly, or None. Rent is paid by Direct Debit or from credit (with or
    without Direct Debit as a backup); other payers set one of those up first (06 §6.2)."""
    method = member.payment_method
    if method == Member.PaymentMethod.CREDIT:
        return None
    if method == Member.PaymentMethod.DIRECT_DEBIT:
        return None if member.mandate_id else "Your Direct Debit isn't set up yet."
    return "Locker rent is paid monthly by Direct Debit or from your member credit. Set one of those up first."


@transaction.atomic
def take_first_rent(rental, today: date) -> int:
    """Take the first rent for a newly rented locker: from the rental's start to the member's next payment day, from
    credit or by Direct Debit. From then on it's billed with everything else on their payment day. Returns what was
    taken (0 if nothing was). Raises LockerError if it can't be paid; the caller's transaction then undoes the rental.
    """
    from apps.storage import rules as storage

    member = Member.objects.select_for_update().get(pk=rental.member_id)
    problem = rent_payment_problem(member)
    if problem:
        raise storage.LockerError(problem)
    if member.payment_day is None:  # an honorary member, who pays no subscription: today becomes their payment day
        member.payment_day = membership.payment_day_for(today)
        member.save(update_fields=["payment_day"])
    monthly = storage.rental_rent_pence(rental, today)
    amount, _ = storage.first_rent(member, monthly or 0, today)
    if amount < MINIMUM_FIRST_RENT_PENCE:
        return 0
    charge = Charge.objects.create(
        member=member,
        kind=Charge.Kind.LOCKER_RENT,
        rental=rental,
        charge_date=today,
        amount_pence=amount,
        status=Charge.Status.DUE,
        first_rent=True,
    )
    if member.payment_method == Member.PaymentMethod.CREDIT:
        try:
            services.pay_from_credit(member, charge.reason, amount, charge=charge, reference=_ref(charge))
        except services.InsufficientCredit as error:
            if not member.mandate_id:
                raise storage.LockerError(
                    f"The first rent is £{_pounds(amount)} and you have £{_pounds(error.credit)} of credit. "
                    "Top up your credit first."
                ) from error
        else:
            membership.charge_paid(charge, today)
            return amount
    try:
        _bill_by_direct_debit(member, [charge])
    except gocardless.GoCardlessError as error:
        logger.warning("Member %s: first rent not taken: %s", member.pk, error)
        raise storage.LockerError(
            "We couldn't set up the Direct Debit for the first rent just now. Please try again in a few minutes."
        ) from error
    return amount
