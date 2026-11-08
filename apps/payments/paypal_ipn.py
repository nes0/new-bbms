"""
What each PayPal notification (IPN) does (spec 04 §4.4).

The view checks a notification with PayPal, then `store` keeps it (ignoring a repeat) and `process_pending` handles
the stored ones in the order they arrived. One that fails is kept with its error for someone to look at (admin:
Payments → PayPal notifications, "try again").

| PayPal says | What happens |
|---|---|
| `subscr_signup` | The member pays by PayPal from now on; one still setting up (or leaving) is activated. |
| `subscr_payment`, `Completed` | Recorded once: pays the oldest due or failed subscription charge, or goes to credit. |
| `web_accept`, `Completed` | A credit top-up, recorded once. |
| `Refunded`, `Reversed`, `Canceled_Reversal` | A correcting entry linked to the original payment; finance is told. |
| `subscr_failed` | The member is emailed; PayPal tries again by itself. |
| `subscr_cancel`, `subscr_eot` | A member paying by that subscription starts leaving. |

Someone BBMS can't match is recorded as a donation with no member; finance is told and the payer is emailed. A
member who has left isn't rejoined: the money goes to their credit and finance is told.
"""

import hashlib
import json
import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.members.models import Member

from . import paypal, services
from .models import Payment, PayPalNotification

logger = logging.getLogger(__name__)

FINANCE = "finance"
REVERSALS = ("Refunded", "Reversed", "Canceled_Reversal")


class Problem(Exception):
    """A notification BBMS won't act on by itself. It's kept, with this message, for a person to look at."""


def pence(value) -> int:
    try:
        return int((Decimal(str(value or "0")) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation as error:
        raise Problem(f"PayPal sent an amount BBMS can't read: {value!r}") from error


def _key(data) -> str:
    """The same notification sent again has the same key; a payment changing status gets a new one."""
    if data.get("txn_id"):
        return f"txn:{data['txn_id']}:{data.get('payment_status', '')}"
    if data.get("ipn_track_id"):
        return f"track:{data['ipn_track_id']}"
    return "hash:" + hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def store(data) -> PayPalNotification | None:
    """Keep a notification PayPal has confirmed. Returns None for one already received."""
    notification, created = PayPalNotification.objects.get_or_create(
        key=_key(data)[:255],
        defaults={
            "txn_type": data.get("txn_type", "")[:50],
            "txn_id": data.get("txn_id", "")[:50],
            "payment_status": data.get("payment_status", "")[:50],
            "subscr_id": data.get("subscr_id", "")[:50],
            "payer_email": data.get("payer_email", "")[:255],
            "receiver_email": (data.get("receiver_email") or data.get("business", ""))[:255],
            "data": data,
        },
    )
    return notification if created else None


def process_pending(retry_failed=False, today=None, only=None) -> int:
    """Handle stored notifications in the order they arrived. Returns how many were handled.

    Failed ones are left alone unless `retry_failed` (the admin's "try again", for the ids in `only`).
    """
    pending = PayPalNotification.objects.filter(processed_at__isnull=True)
    if not retry_failed:
        pending = pending.filter(error="")
    if only is not None:
        pending = pending.filter(pk__in=only)
    count = 0
    for notification_id in pending.order_by("received_at", "pk").values_list("pk", flat=True):
        try:
            with transaction.atomic():
                notification = PayPalNotification.objects.select_for_update().get(pk=notification_id)
                if notification.processed_at:
                    continue
                notification.outcome = (handle(notification, today or timezone.localdate()) or "logged")[:255]
                notification.processed_at = timezone.now()
                notification.error = ""
                notification.save()
            count += 1
        except Exception as error:  # keep going; a person looks at the failed one
            if not isinstance(error, Problem):
                logger.exception("PayPal notification %s failed", notification_id)
            else:
                logger.warning("PayPal notification %s needs looking at: %s", notification_id, error)
            PayPalNotification.objects.filter(pk=notification_id).update(error=f"{error}")
    return count


# --- the rules ---


def find_member(data):
    """By the subscription BBMS knows, then the reference on BBMS's buttons, then the payer's email address."""
    subscr_id = data.get("subscr_id", "")
    if subscr_id:
        member = Member.objects.filter(paypal_subscription_id=subscr_id).first()
        if member:
            return member
    member_id = paypal.member_id_from(data.get("custom", ""))
    if member_id:
        member = Member.objects.filter(pk=member_id).first()
        if member:
            return member
    email = data.get("payer_email", "").strip()
    if email:
        return Member.objects.filter(Q(email__iexact=email) | Q(paypal_email__iexact=email)).order_by("pk").first()
    return None


def handle(notification: PayPalNotification, today) -> str:
    data = notification.data
    receiver = notification.receiver_email.strip().lower()
    if receiver not in paypal.accepted_accounts():
        raise Problem(f"Not for the space's PayPal account (it's for {receiver or 'no account'})")
    if data.get("mc_currency") and data["mc_currency"] != "GBP":
        raise Problem(f"Not in pounds ({data['mc_currency']})")
    status = data.get("payment_status", "")
    txn_type = data.get("txn_type", "")
    if status in REVERSALS:
        return _reversal(notification, data, status, today)
    member = find_member(data)
    notification.member = member
    if txn_type == "subscr_signup":
        if member is None:
            return "no member matches: nothing set up"
        return _set_up(member, data.get("subscr_id", ""), today, signup=True)
    if txn_type in ("subscr_payment", "web_accept"):
        if status != "Completed":
            return f"payment {status.lower() or 'with no status'}: waiting for PayPal to complete it"
        return _payment(notification, member, data, today)
    if txn_type == "subscr_failed":
        if member is not None:
            from apps.billing import emails

            emails.send(member, "paypal_failed")
        return "PayPal couldn't take a payment; it tries again by itself"
    if txn_type in ("subscr_cancel", "subscr_eot"):
        return _cancelled(member, data.get("subscr_id", ""))
    return ""


def _set_up(member, subscr_id, today, *, signup) -> str:
    """The member pays by PayPal from now on (03 §3.1)."""
    from apps.billing import membership

    member = Member.objects.select_for_update().get(pk=member.pk)
    if member.status == Member.Status.LEFT or member.banned:
        return f"member {member.pk} has left: not set up (they rejoin first)"
    if subscr_id and (signup or not member.paypal_subscription_id):
        member.paypal_subscription_id = subscr_id
    member.payment_method = Member.PaymentMethod.PAYPAL
    if member.payment_day is None:
        member.payment_day = membership.payment_day_for(today)
    member.save(update_fields=["paypal_subscription_id", "payment_method", "payment_day"])
    if member.status in (Member.Status.SETTING_UP, Member.Status.LEAVING):
        membership.activate(member, today)
        return f"member {member.pk}: PayPal subscription set up"
    return f"member {member.pk}: PayPal subscription recorded"


def _record(member, reason, amount, fields, note=""):
    return Payment.objects.create(
        member=member,
        reason=reason,
        source=Payment.Source.PAYPAL,
        status=Payment.Status.PAID,
        amount_pence=amount,
        paid_at=timezone.now(),
        note=note,
        **fields,
    )


def _payment(notification, member, data, today) -> str:
    from apps.billing import billing, emails
    from apps.billing.models import Charge

    txn_id = data.get("txn_id", "")
    if not txn_id:
        raise Problem("A payment with no transaction id")
    if Payment.objects.filter(source=Payment.Source.PAYPAL, source_id=txn_id).exists():
        return "already recorded"
    amount = pence(data.get("mc_gross"))
    if amount <= 0:
        raise Problem(f"A payment of £{amount / 100:.2f}")
    fields = {"source_id": txn_id, "fee_pence": max(pence(data.get("mc_fee")), 0)}
    money = f"{amount / 100:.2f}"

    if member is None:
        payment = _record(None, Payment.Reason.DONATION, amount, fields, note="PayPal payer BBMS couldn't match")
        notification.payment = payment
        payer = data.get("payer_email", "")
        if payer:
            emails.send_to_address(payer, "paypal_unknown_payer", amount=money, payment=payment)
        emails.tell_role(FINANCE, "paypal_unknown_payment", amount=money, payment=payment)
        return f"no member matches: recorded as a donation (payment {payment.pk})"

    if member.status == Member.Status.LEFT:
        payment = services.record_payment(
            member, Payment.Reason.BALANCE, Payment.Source.PAYPAL, amount, note="Paid after leaving", **fields
        )
        notification.payment = payment
        emails.tell_role(FINANCE, "paypal_after_leaving", member=member, amount=money, payment=payment)
        return f"member {member.pk} has left: added to their credit (payment {payment.pk})"

    if data.get("txn_type") == "web_accept":
        low, high = _limits()
        if not low <= amount <= high:
            logger.warning("PayPal top-up %s for member %s is outside the limits; recorded", txn_id, member.pk)
        payment = services.record_payment(member, Payment.Reason.BALANCE, Payment.Source.PAYPAL, amount, **fields)
        notification.payment = payment
        return f"member {member.pk}: credit top-up (payment {payment.pk})"

    # A subscription payment.
    if member.status == Member.Status.SETTING_UP:
        _set_up(member, data.get("subscr_id", ""), today, signup=False)
    elif data.get("subscr_id") and not member.paypal_subscription_id:
        Member.objects.filter(pk=member.pk).update(paypal_subscription_id=data["subscr_id"])
    charge = (
        Charge.objects.filter(member=member, kind=Charge.Kind.SUBSCRIPTION, status__in=Charge.UNPAID)
        .order_by("charge_date", "pk")
        .first()
    )
    if charge is not None:
        payment = billing.pay_charge(charge, Payment.Source.PAYPAL, today, amount_pence=amount, **fields)
        notification.payment = payment
        return f"member {member.pk}: paid charge {charge.pk} (payment {payment.pk})"
    payment = services.record_payment(
        member, Payment.Reason.BALANCE, Payment.Source.PAYPAL, amount, note="PayPal subscription payment", **fields
    )
    notification.payment = payment
    return f"member {member.pk}: no charge waiting, so added to credit (payment {payment.pk})"


def _limits():
    from .checkout import top_up_limits

    return top_up_limits()


def _reversal(notification, data, status, today) -> str:
    """A refund or reversal (or a reversal cancelled): a correcting entry, never an edit (04 §4.1)."""
    from apps.billing import emails, membership
    from apps.billing.models import Charge

    parent = data.get("parent_txn_id", "")
    original = (
        Payment.objects.filter(source=Payment.Source.PAYPAL, source_id=parent, corrects__isnull=True)
        .select_related("member", "charge")
        .first()
        if parent
        else None
    )
    if original is None:
        raise Problem(f"PayPal says {status.lower()} for a payment BBMS doesn't have ({parent or 'no id'})")
    txn_id = data.get("txn_id", "")
    if txn_id and Payment.objects.filter(source=Payment.Source.PAYPAL, source_id=txn_id).exists():
        return "already recorded"
    amount = abs(pence(data.get("mc_gross")))
    if status != "Canceled_Reversal":
        amount = -amount
    notification.member = original.member
    payment = _record(
        original.member,
        original.reason,
        amount,
        {"source_id": txn_id, "corrects": original, "charge": original.charge, "reference": original.reference},
        note=f"PayPal: {status.replace('_', ' ').lower()}",
    )
    notification.payment = payment
    charge = original.charge
    if charge is not None:
        charge = Charge.objects.select_for_update(of=("self",)).select_related("member").get(pk=charge.pk)
        if amount < 0 and charge.status == Charge.Status.PAID:
            membership.charge_failed(charge, today)
        elif amount > 0 and charge.status == Charge.Status.FAILED:
            membership.charge_paid(charge, today)
    emails.tell_role(
        FINANCE,
        "paypal_reversal",
        member=original.member,
        status=status.replace("_", " ").lower(),
        amount=f"{amount / 100:.2f}",
        original=original,
        payment=payment,
    )
    return f"{status.replace('_', ' ').lower()}: correction {payment.pk} for payment {original.pk}"


def _cancelled(member, subscr_id) -> str:
    """Only the subscription the member pays by now: cancelling an old one (after switching) changes nothing."""
    from apps.billing import membership

    if member is None:
        return "no member matches"
    if member.payment_method != Member.PaymentMethod.PAYPAL:
        return f"member {member.pk} doesn't pay by PayPal now: nothing to do"
    if member.paypal_subscription_id and member.paypal_subscription_id != subscr_id:
        return f"member {member.pk}: an old subscription; nothing to do"
    if member.status not in (Member.Status.ACTIVE, Member.Status.SUSPENDED):
        return f"member {member.pk} is {member.get_status_display().lower()}: nothing to do"
    membership.start_leaving(member, cancel_mandate=False, paypal_cancelled=True)
    return f"member {member.pk}: PayPal subscription ended, so they're leaving"
