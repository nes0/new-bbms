"""
Everything that moves money goes through these functions, so the rules live in one place (spec 04).

- The amount for a fixed-price reason is always worked out here, never taken from a form or the browser.
- Ledger rows are never edited to change money; corrections are new rows (`settle_session`).
"""

from django.db import transaction
from django.utils import timezone

from apps.members.models import Member

from . import prices
from .credit import credit_pence
from .models import Payment


class InsufficientCredit(Exception):
    def __init__(self, credit, amount, minimum):
        self.credit, self.amount, self.minimum = credit, amount, minimum
        super().__init__(f"Credit £{credit / 100:.2f} isn't enough to pay £{amount / 100:.2f}.")


def _lock(member):
    """Hold the member's row until the transaction ends, so two payments from credit can't race each other."""
    Member.objects.select_for_update().filter(pk=member.pk).first()


@transaction.atomic
def record_payment(member, reason, source, amount_pence, *, status=Payment.Status.PAID, **fields) -> Payment:
    """Record money received (or a credit top-up) as one ledger row."""
    if amount_pence <= 0:
        raise ValueError("A payment must be for more than £0.")
    if status == Payment.Status.PAID:
        fields.setdefault("paid_at", timezone.now())  # a bank line gives the day the money arrived
    payment = Payment.objects.create(
        member=member, reason=reason, source=source, status=status, amount_pence=amount_pence, **fields
    )
    if reason == Payment.Reason.STORAGE_BOX and status in (Payment.Status.PAID, Payment.Status.PENDING):
        from apps.storage import rules as storage

        storage.give_box_flag(member, None, payment=payment)  # whichever way it was paid (06 §6.2)
    return payment


@transaction.atomic
def pay_from_credit(member, reason, amount_pence, *, minimum_pence=prices.MINIMUM_CREDIT_PENCE, **fields) -> Payment:
    """Spend the member's credit, refusing if it would go below the minimum (£0 for everything but equipment)."""
    if amount_pence <= 0:
        raise ValueError("A payment must be for more than £0.")
    _lock(member)
    credit = credit_pence(member)
    if credit - amount_pence < minimum_pence:
        raise InsufficientCredit(credit, amount_pence, minimum_pence)
    return record_payment(member, reason, Payment.Source.BALANCE, amount_pence, **fields)


def pay(member, reason, source, amount_pence, **fields) -> Payment:
    """Pay by any method: from credit (with its minimum), or recorded as received."""
    if source == Payment.Source.BALANCE:
        return pay_from_credit(member, reason, amount_pence, **fields)
    return record_payment(member, reason, source, amount_pence, **fields)


@transaction.atomic
def pay_induction(member, equipment, source, *, amount_pence=None, **fields) -> Payment:
    """Pay a machine's induction fee: the member then waits for a trainer to sign them off (05 §5.8).

    `amount_pence` is only for a card payment, which records what the member was actually charged at checkout.
    """
    from apps.equipment.models import Induction

    if not equipment.induction_fee_pence:
        raise ValueError(f"{equipment} has no induction fee.")
    payment = pay(
        member,
        Payment.Reason.INDUCTION,
        source,
        amount_pence or equipment.induction_fee_pence,
        reference=equipment.induction_key,
        **fields,
    )
    induction, _ = Induction.objects.get_or_create(member=member, category=equipment.induction_key)
    induction.paid = True
    induction.payment = payment
    induction.save(update_fields=["paid", "payment"])
    return payment


def pay_key_deposit(member, source, *, amount_pence=None, **fields) -> Payment:
    # TODO(members): record the deposit on the member (key issued date, number, returned) (02 §2.2).
    amount = amount_pence or prices.key_deposit_pence()
    return pay(member, Payment.Reason.KEY_DEPOSIT, source, amount, **fields)


def pay_box_fee(member, source, *, amount_pence=None, **fields) -> Payment:
    """Pay the one-off box fee. Recording it sets the member's "box paid" flag (06 §6.2)."""
    amount = amount_pence or prices.box_fee_pence()
    return pay(member, Payment.Reason.STORAGE_BOX, source, amount, reference="box", **fields)


def has_paid(member, reason) -> bool:
    """Whether the member has a paid (or pending) payment for a one-off reason, e.g. the key deposit."""
    return member.payments.filter(reason=reason, status__in=(Payment.Status.PAID, Payment.Status.PENDING)).exists()


@transaction.atomic
def settle_session(session):
    """Make what the member has been charged for a machine session match its fee (05 §5.4, §5.6).

    Called when a session ends, and again whenever its fee might have changed (times adjusted, reclassified as
    training, merged with the next session). Only the difference is posted, as a new row linked to the session: a
    charge, or a refund (a negative amount). Calling it twice changes nothing. Equipment fees have no credit minimum:
    a session is never refused or stopped at this point (decided 2026-11-07).

    Returns the new payment, or None if nothing needed to change.
    """
    if session.ended_at is None:
        return None
    charges = list(session.payments.filter(reason=Payment.Reason.EQUIPMENT_FEE).order_by("pk"))
    if session.legacy_id and session.charged_at and not charges:
        return None  # settled by the legacy system before this ledger existed; history only
    owed = 0 if session.removed else (session.fee_pence or 0)
    charged = sum(p.amount_pence for p in charges if p.status in Payment.CREDIT_STATUSES)
    now = timezone.now()
    payment = None
    if owed != charged:
        payment = Payment.objects.create(
            member=session.member,
            reason=Payment.Reason.EQUIPMENT_FEE,
            source=Payment.Source.BALANCE,
            status=Payment.Status.PAID,
            amount_pence=owed - charged,
            reference=f"{session.pk}:{session.equipment.slug}",
            usage_session=session,
            corrects=charges[0] if charges else None,
            paid_at=now,
        )
    if session.charged_at is None:
        session.charged_at = now
        session.save(update_fields=["charged_at"])
    return payment


def _close_withdrawal(withdrawal, status, by, note=""):
    withdrawal.status = status
    withdrawal.handled_by = by
    withdrawal.handled_at = timezone.now()
    withdrawal.note = note or withdrawal.note
    # Bank details are only kept until the request has been dealt with.
    withdrawal.account_name = withdrawal.sort_code = withdrawal.account_number = ""
    withdrawal.save()


@transaction.atomic
def pay_withdrawal(withdrawal, by) -> Payment:
    """Finance has paid a withdrawal request: take it from the member's credit (04 §4.7)."""
    payment = pay_from_credit(
        withdrawal.member,
        Payment.Reason.WITHDRAWAL,
        withdrawal.amount_pence,
        created_by=by,
        reference=f"withdrawal-request:{withdrawal.pk}",
    )
    withdrawal.payment = payment
    _close_withdrawal(withdrawal, withdrawal.Status.PAID, by)
    return payment


def reject_withdrawal(withdrawal, by, note=""):
    _close_withdrawal(withdrawal, withdrawal.Status.REJECTED, by, note)


# --- finance putting payments right (04 §4.8) ---


class CantCorrect(Exception):
    """Why a payment can't be assigned, refunded or cancelled. The message is shown to finance."""


def _note(payment, text):
    payment.note = (payment.note + "\n" if payment.note else "") + text


def _locked(payment) -> Payment:
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.corrections.exists():
        raise CantCorrect("This payment has already been put right.")
    if payment.status != Payment.Status.PAID:
        raise CantCorrect("Only a paid payment can be changed.")
    return payment


def is_unmatched(payment) -> bool:
    """Money nobody could match to a member (an unknown PayPal payer, 04 §4.4)."""
    return payment.member_id is None and payment.reason == Payment.Reason.DONATION


def can_refund_to_credit(payment) -> bool:
    return (
        payment.member_id is not None
        and payment.reason in (Payment.Reason.DONATION, Payment.Reason.INDUCTION)
        and payment.amount_pence > 0
        and payment.status == Payment.Status.PAID
        and payment.corrects_id is None
    )


def can_cancel(payment) -> bool:
    """Only cash put into credit, or cash paid out of it, by an admin: to undo a mistake (04 §4.8)."""
    cash_in = payment.source == Payment.Source.CASH and payment.reason == Payment.Reason.BALANCE
    cash_out = payment.source == Payment.Source.BALANCE and payment.reason == Payment.Reason.WITHDRAWAL
    return (cash_in or cash_out) and payment.amount_pence > 0 and payment.corrects_id is None


@transaction.atomic
def assign_unmatched(payment, member, by) -> Payment:
    """Unmatched money becomes a top-up of the member's credit (04 §4.1, §4.8)."""
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if not is_unmatched(payment):
        raise CantCorrect("This payment already belongs to a member.")
    payment.member, payment.reason = member, Payment.Reason.BALANCE
    _note(payment, f"Assigned to {member} by {by}.")
    payment.save(update_fields=["member", "reason", "note"])
    return payment


def _reverse(payment, by, note) -> Payment:
    return Payment.objects.create(
        member=payment.member,
        reason=payment.reason,
        source=payment.source,
        amount_pence=-payment.amount_pence,
        reference=payment.reference,
        corrects=payment,
        created_by=by,
        paid_at=timezone.now(),
        note=note,
    )


@transaction.atomic
def refund_to_credit(payment, by, note="") -> Payment:
    """Give a donation or an induction fee back as credit. The original row stays; a correcting row cancels it and
    the money goes to the member's credit. Returns the credit row.

    An induction fee can only be refunded before the member is trained; the induction is then unpaid again.
    """
    from apps.equipment.models import Induction

    if not can_refund_to_credit(payment):
        raise CantCorrect("Only a donation or an induction fee can be refunded to credit.")
    payment = _locked(payment)
    if payment.reason == Payment.Reason.INDUCTION:
        induction = Induction.objects.select_for_update().filter(payment=payment).first()
        if induction and induction.trained_at:
            raise CantCorrect("They've had the induction, so the fee can't be refunded.")
        if induction:
            induction.paid, induction.payment = False, None
            induction.save(update_fields=["paid", "payment"])
    note = note or f"Refunded to credit by {by}."
    reversal = _reverse(payment, by, note)
    if payment.source == Payment.Source.BALANCE:
        return reversal  # paid from credit: reversing it puts the money back
    return Payment.objects.create(
        member=payment.member,
        reason=Payment.Reason.BALANCE,
        source=payment.source,
        amount_pence=payment.amount_pence,
        reference=payment.reference,
        corrects=payment,
        created_by=by,
        paid_at=timezone.now(),
        note=note,
    )


@transaction.atomic
def cancel_payment(payment, by, note="") -> Payment:
    """Undo cash recorded by mistake, with a correcting row (the original stays, so the history shows both)."""
    if not can_cancel(payment):
        raise CantCorrect("Only cash put into or paid out of credit can be cancelled.")
    payment = _locked(payment)
    return _reverse(payment, by, note or f"Cancelled by {by}.")
