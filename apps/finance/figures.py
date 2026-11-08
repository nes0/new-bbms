"""
The finance overview's figures (spec 04 §4.8), worked out from the ledger and the charges each time.

Sums count payments that are paid, pending or withdrawn (the statuses that count towards credit), and include
correcting rows, so a refund lowers the total it corrects.
"""

from django.db.models import Sum

from apps.billing.billing import charge_amount
from apps.billing.models import Charge
from apps.payments.credit import credit_by_member
from apps.payments.models import Payment, WithdrawalRequest

from .models import BankLine


def _counted():
    return Payment.objects.filter(status__in=Payment.CREDIT_STATUSES)


def _total(query) -> int:
    return query.aggregate(total=Sum("amount_pence"))["total"] or 0


def overview() -> dict:
    counted = _counted()
    credits = credit_by_member().values()
    rent_unpaid = Charge.objects.filter(kind=Charge.Kind.LOCKER_RENT, status__in=Charge.UNPAID).select_related(
        "rental__locker", "member"
    )
    return {
        "credit_paid_in": _total(counted.filter(reason=Payment.Reason.BALANCE)),
        "credit_paid_out": _total(counted.filter(source=Payment.Source.BALANCE)),
        "credit_held": sum(c for c in credits if c > 0),
        "credit_owed": -sum(c for c in credits if c < 0),
        "members_in_credit": sum(1 for c in credits if c > 0),
        "members_below_zero": sum(1 for c in credits if c < 0),
        "box_fees": _total(counted.filter(reason=Payment.Reason.STORAGE_BOX)),
        "locker_rent": _total(counted.filter(reason=Payment.Reason.LOCKER_RENT)),
        "locker_rent_unpaid": sum(charge_amount(c) or 0 for c in rent_unpaid),
        "locker_rent_unpaid_count": len(rent_unpaid),
        "key_deposits": _total(counted.filter(reason=Payment.Reason.KEY_DEPOSIT)),
        # Legacy recorded members' contributions to buying the laser cutter with this reference.
        "laser_investment": _total(counted.filter(reference="laser-cutter")),
        "unmatched_payments": Payment.objects.filter(member__isnull=True, reason=Payment.Reason.DONATION).count(),
        "unmatched_lines": BankLine.objects.filter(kept=True, outcome=BankLine.Outcome.UNMATCHED).count(),
        "withdrawals_waiting": WithdrawalRequest.objects.filter(status=WithdrawalRequest.Status.REQUESTED).count(),
    }
