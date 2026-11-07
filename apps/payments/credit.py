"""
Member credit ("BB Credit"), worked out from the ledger whenever it's needed, never stored (04 §4.7, decided
2026-11-05).

The rule is the legacy one: credit = top-ups (reason `balance`) − spending from credit (source `balance`), counting
payments that are paid, pending or withdrawn. A correcting row with a negative amount simply reverses its sign.
"""

from django.db.models import Case, F, IntegerField, Q, Sum, Value, When
from django.db.models.functions import Coalesce

from .models import Payment

_COUNTS = Q(status__in=Payment.CREDIT_STATUSES)
_IN = Case(When(_COUNTS & Q(reason=Payment.Reason.BALANCE), then=F("amount_pence")), default=Value(0))
_OUT = Case(When(_COUNTS & Q(source=Payment.Source.BALANCE), then=F("amount_pence")), default=Value(0))
CREDIT_EXPRESSION = Coalesce(Sum(_IN), 0, output_field=IntegerField()) - Coalesce(
    Sum(_OUT), 0, output_field=IntegerField()
)


def credit_pence(member) -> int:
    """The member's credit now, in pence. Can be negative (equipment fees may take it below zero)."""
    return Payment.objects.filter(member=member).aggregate(credit=CREDIT_EXPRESSION)["credit"]


def credit_movements(member):
    """The payments that changed the member's credit, newest first (for their credit page)."""
    return Payment.objects.filter(member=member, status__in=Payment.CREDIT_STATUSES).filter(
        Q(reason=Payment.Reason.BALANCE) | Q(source=Payment.Source.BALANCE)
    )


def credit_by_member():
    """{member id: credit} for every member with any credit movement. Used for the credit liability (04 §4.8)."""
    rows = Payment.objects.filter(member__isnull=False).values("member").annotate(credit=CREDIT_EXPRESSION)
    return {row["member"]: row["credit"] for row in rows if row["credit"]}
