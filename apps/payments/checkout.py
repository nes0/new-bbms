"""
What a member can pay for themselves, and paying it by card or from credit (spec 04 §4.1 "where a member can pay
what", §4.3).

| Payment for | Card | Credit |
|---|---|---|
| A subscription or locker rent charge that's due or failed | yes | yes |
| Induction fee | yes | yes |
| Key deposit (trusted members) | yes | yes |
| Box fee | yes | yes |
| Credit top-up | yes | — |

Every amount is worked out here from what's being paid for, never taken from the browser. A card payment is
recorded when Stripe says it's paid: by its webhook, or when the member comes back to BBMS, whichever is first; both
are safe to happen twice. If what it was for has been paid some other way meanwhile, the money goes to the member's
credit instead, so it's never lost.
"""

import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.site_settings.models import SiteSettings

from . import cards, prices, services
from .models import CardCheckout, Payment, StripeEvent

logger = logging.getLogger(__name__)

Purpose = CardCheckout.Purpose


@dataclass
class Payable:
    purpose: str
    label: str
    amount_pence: int
    key: str  # identifies it in a form: "key-deposit", "storage-box", "induction:<equipment id>", "charge:<id>"
    equipment: object = None
    charge: object = None


def payables(member) -> list[Payable]:
    """Everything this member can pay for now, with the price BBMS charges."""
    from apps.billing import billing
    from apps.billing.models import Charge
    from apps.equipment.models import Equipment

    items = []
    for charge in member.charges.filter(status__in=Charge.UNPAID).select_related("rental__locker"):
        amount = billing.charge_amount(charge)
        if amount:
            what = "Subscription" if charge.rental_id is None else f"Rent for {charge.rental.locker}"
            label = f"{what}, {charge.charge_date.day} {charge.charge_date:%B %Y}"
            items.append(Payable(Purpose.CHARGE, label, amount, f"charge:{charge.pk}", charge=charge))
    if not member.has_space_access:
        return items
    if member.trusted and not services.has_paid(member, Payment.Reason.KEY_DEPOSIT):
        items.append(Payable(Purpose.KEY_DEPOSIT, "Key deposit", prices.key_deposit_pence(), "key-deposit"))
    if not services.has_paid(member, Payment.Reason.STORAGE_BOX):
        items.append(Payable(Purpose.STORAGE_BOX, "Box fee", prices.box_fee_pence(), "storage-box"))
    paid_categories = set(member.inductions.filter(paid=True).values_list("category", flat=True))
    seen = set()
    machines = Equipment.objects.filter(archived=False, requires_induction=True, induction_fee_pence__gt=0)
    for machine in machines.order_by("name"):
        if machine.induction_key in paid_categories or machine.induction_key in seen:
            continue
        seen.add(machine.induction_key)
        label = f"Induction: {machine.name}"
        items.append(Payable(Purpose.INDUCTION, label, machine.induction_fee_pence, f"induction:{machine.pk}", machine))
    return items


def find_payable(member, key) -> Payable | None:
    return next((item for item in payables(member) if item.key == key), None)


def top_up_limits() -> tuple[int, int]:
    config = SiteSettings.load()
    return config.card_top_up_minimum_pence, config.card_top_up_maximum_pence


# --- paying ---


def _record(member, purpose, source, amount, *, equipment=None, charge=None, today=None, **fields) -> Payment:
    """One ledger row (or more) for what's been paid, by any method."""
    from apps.billing import billing

    if purpose == Purpose.TOP_UP:
        return services.record_payment(member, Payment.Reason.BALANCE, source, amount, **fields)
    if purpose == Purpose.INDUCTION:
        return services.pay_induction(member, equipment, source, amount_pence=amount, **fields)
    if purpose == Purpose.KEY_DEPOSIT:
        return services.pay_key_deposit(member, source, amount_pence=amount, **fields)
    if purpose == Purpose.STORAGE_BOX:
        return services.pay_box_fee(member, source, amount_pence=amount, **fields)
    if purpose == Purpose.CHARGE:
        return billing.pay_charge(charge, source, today or timezone.localdate(), amount_pence=amount, **fields)
    raise ValueError(f"Unknown purpose {purpose!r}")


def pay_from_credit(member, item: Payable) -> Payment:
    """Raises services.InsufficientCredit if it would take credit below £0."""
    return _record(
        member, item.purpose, Payment.Source.BALANCE, item.amount_pence, equipment=item.equipment, charge=item.charge
    )


def start_card_checkout(member, *, purpose, amount_pence, label, success_url, cancel_url, equipment=None, charge=None):
    """Create the checkout at Stripe. Returns the URL to send the member to. Raises cards.CardError."""
    checkout = CardCheckout.objects.create(
        member=member, purpose=purpose, amount_pence=amount_pence, equipment=equipment, charge=charge
    )
    try:
        session = cards.client().create_checkout(
            amount_pence=amount_pence,
            description=f"{SiteSettings.load().space_name}: {label}",
            email=member.email,
            reference=str(checkout.pk),
            success_url=success_url,
            cancel_url=cancel_url,
        )
    except cards.CardError:
        checkout.status = CardCheckout.Status.EXPIRED
        checkout.note = "Stripe couldn't start it"
        checkout.save(update_fields=["status", "note"])
        raise
    checkout.session_id = session.id
    checkout.save(update_fields=["session_id"])
    return session.url


def _still_needed(checkout) -> bool:
    from apps.billing.models import Charge

    member = checkout.member
    if checkout.purpose == Purpose.CHARGE:
        return Charge.objects.filter(pk=checkout.charge_id, status__in=Charge.UNPAID).exists()
    if checkout.purpose == Purpose.KEY_DEPOSIT:
        return not services.has_paid(member, Payment.Reason.KEY_DEPOSIT)
    if checkout.purpose == Purpose.STORAGE_BOX:
        return not services.has_paid(member, Payment.Reason.STORAGE_BOX)
    if checkout.purpose == Purpose.INDUCTION:
        return not member.inductions.filter(category=checkout.equipment.induction_key, paid=True).exists()
    return True


def fulfil(checkout: CardCheckout, result: cards.Result) -> Payment | None:
    """Record a checkout Stripe says is paid (or mark one that expired). Safe to call more than once."""
    with transaction.atomic():
        checkout = (
            CardCheckout.objects.select_for_update(of=("self",))
            .select_related("member", "equipment")
            .get(pk=checkout.pk)
        )
        if checkout.status == CardCheckout.Status.PAID:
            return checkout.payment
        if not result.paid:
            if result.expired:
                checkout.status = CardCheckout.Status.EXPIRED
                checkout.save(update_fields=["status"])
            return None
        purpose, amount, note = checkout.purpose, result.amount_pence or checkout.amount_pence, ""
        if amount != checkout.amount_pence:
            note = f"Stripe took £{amount / 100:.2f}, not £{checkout.amount_pence / 100:.2f}: added to credit"
            purpose = Purpose.TOP_UP
        elif not _still_needed(checkout):
            note = f"{checkout.get_purpose_display()} was already paid, so this went to credit"
            purpose = Purpose.TOP_UP
        if note:
            logger.warning("Card checkout %s: %s", checkout.pk, note)
        if result.fee_pence is None:
            logger.warning("Card checkout %s: Stripe's fee wasn't ready; recorded as £0", checkout.pk)
        payment = _record(
            checkout.member,
            purpose,
            Payment.Source.STRIPE,
            amount,
            equipment=checkout.equipment,
            charge=checkout.charge,
            source_id=result.payment_id,
            fee_pence=result.fee_pence or 0,
            note=note,
        )
        checkout.status = CardCheckout.Status.PAID
        checkout.paid_at = timezone.now()
        checkout.payment = payment
        checkout.note = note
        checkout.save(update_fields=["status", "paid_at", "payment", "note"])
        return payment


def check_with_stripe(checkout: CardCheckout) -> Payment | None:
    """Ask Stripe how a checkout went, and record it if it's paid. Raises cards.CardError."""
    if checkout.status == CardCheckout.Status.PAID or not checkout.session_id:
        return checkout.payment
    return fulfil(checkout, cards.client().result(checkout.session_id))


# --- Stripe's webhook ---

HANDLED = {
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed",
    "checkout.session.expired",
}


def handle_event(event_id, event_type, session_id) -> str:
    """One webhook event. A repeat of one already handled is ignored."""
    record, created = StripeEvent.objects.get_or_create(
        event_id=event_id, defaults={"type": event_type, "object_id": session_id}
    )
    if record.processed_at:
        return "already handled"
    try:
        outcome = _handle(event_type, session_id)
    except Exception as error:
        logger.exception("Stripe event %s failed", event_id)
        record.error = f"{type(error).__name__}: {error}"
        record.save(update_fields=["error"])
        raise
    record.outcome, record.error, record.processed_at = outcome, "", timezone.now()
    record.save(update_fields=["outcome", "error", "processed_at"])
    return outcome


def _handle(event_type, session_id) -> str:
    if event_type not in HANDLED:
        return "logged"
    checkout = CardCheckout.objects.filter(session_id=session_id).first()
    if checkout is None:
        return "not a checkout BBMS started"
    payment = check_with_stripe(checkout)
    checkout.refresh_from_db()
    return f"checkout {checkout.pk}: {checkout.get_status_display().lower()}" + (
        f" (payment {payment.pk})" if payment else ""
    )
