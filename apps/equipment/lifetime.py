"""
A machine's whole life in figures, for reviewing retired equipment (spec 05 §5.7, "Retired equipment").

An access fee is a one-off payment, nominally towards the machine's eventual replacement, so the question for a
retired machine is whether what it brought in (access fees and usage fees) covered what it cost (its price, where
known, and the expenses claimed for it). Money is counted the same way as the finance pages (`apps.finance.money`).
"""

from dataclasses import dataclass, field

from django.db.models import Count, Sum
from django.utils import timezone

from apps.finance.money import COUNTED, _approved_expenses
from apps.payments.models import Payment

from . import rules
from .models import Equipment, Induction


@dataclass
class Lifetime:
    machine: Equipment
    access_fees: int = 0
    usage_fees: int = 0
    expenses: int = 0
    sessions: int = 0
    users: int = 0
    trained: int = 0
    hours: float = 0.0
    shares_access_with: list = field(default_factory=list)  # other machines on the same induction (same access fee)

    @property
    def income(self) -> int:
        return self.access_fees + self.usage_fees

    @property
    def net(self) -> int:
        """Income less expenses and, where it's known, the purchase price."""
        return self.income - self.expenses - (self.machine.purchase_price_pence or 0)

    @property
    def years(self):
        start, end = self.machine.obtained_at, self.machine.removed_at or timezone.localdate()
        if start is None:
            return None
        return max(0.0, (end - start).days / 365.25)


def retired():
    return Equipment.objects.filter(archived=True).order_by("-removed_at", "name")


def lifetime(machine: Equipment) -> Lifetime:
    life = Lifetime(machine)
    payments = Payment.objects.filter(status__in=COUNTED)
    if machine.requires_induction:
        life.access_fees = (
            payments.filter(reason=Payment.Reason.INDUCTION, reference=machine.induction_key).aggregate(
                t=Sum("amount_pence")
            )["t"]
            or 0
        )
        life.trained = Induction.objects.filter(
            category=machine.induction_key, paid=True, trained_at__isnull=False
        ).count()
        life.shares_access_with = [
            other
            for other in Equipment.objects.exclude(pk=machine.pk).filter(requires_induction=True)
            if other.induction_key == machine.induction_key
        ]
    life.usage_fees = (
        payments.filter(reason=Payment.Reason.EQUIPMENT_FEE, usage_session__equipment=machine).aggregate(
            t=Sum("amount_pence")
        )["t"]
        or 0
    )
    life.expenses = _approved_expenses().filter(equipment=machine).aggregate(t=Sum("amount_pence"))["t"] or 0
    used = machine.sessions.filter(removed=False, ended_at__isnull=False).aggregate(
        n=Count("pk"), people=Count("member", distinct=True)
    )
    life.sessions, life.users = used["n"], used["people"]
    life.hours = rules.running_seconds(machine) / 3600
    return life


def has_history(machine: Equipment) -> bool:
    """Whether deleting it would lose anything: sessions, training, money or expenses. Such a machine is retired."""
    return (
        machine.sessions.exists()
        or machine.service_records.exists()
        or machine.expenses.exists()
        or (machine.requires_induction and Induction.objects.filter(category=machine.induction_key).exists())
    )
