"""
What the space took and spent, by heading (spec 04 §4.9), and what each machine has made (requested by the
trustees, 2026-11-08).

**Income** is counted when it's paid for something, by what it was for, whatever paid it: a laser fee taken from
credit is income when the laser is used, so a credit top-up isn't income too (it's money held for the member, shown
separately). Payments that are paid count (and legacy's "withdrawn"); pending ones (a Direct Debit on its way) are
shown separately. Corrections are negative rows under the heading they correct. Dates are when BBMS recorded the
payment.

**Expenditure** is what BBMS knows about: expenses members claimed back, counted when they were approved. The
space's other spending (rent, bills) isn't in BBMS.

**Machines**: induction fees + usage fees − approved expenses claimed for the machine. Machines that share an
induction (several lasers on one induction, say) are one row, because the induction fee is for all of them.
"""

from collections import defaultdict
from datetime import date, datetime, time, timedelta

from django.db.models import Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from apps.equipment.models import Equipment
from apps.expenses.models import Expense
from apps.payments.models import Payment

R = Payment.Reason
HEADINGS = [
    ("monthly", "Monthly fees", (R.SUBSCRIPTION,)),
    ("oneoff", "One-off payments", (R.INDUCTION, R.STORAGE_BOX, R.KEY_DEPOSIT, R.DONATION)),
    ("use", "Ongoing use", (R.EQUIPMENT_FEE, R.LOCKER_RENT, R.CONSUMABLE, R.COSTS)),
]
HEADING_OF = {reason: key for key, _, reasons in HEADINGS for reason in reasons}
COUNTED = (Payment.Status.PAID, Payment.Status.WITHDRAWN)
REASON_LABELS = dict(R.choices)


def _range(start: date, end: date):
    """Aware datetimes from the start of `start` to the end of `end` (UK time)."""
    return (
        timezone.make_aware(datetime.combine(start, time.min)),
        timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min)),
    )


def _payments(start, end):
    since, until = _range(start, end)
    return Payment.objects.filter(created_at__gte=since, created_at__lt=until)


def _approved_expenses(start=None, end=None):
    expenses = Expense.objects.filter(status=Expense.Status.APPROVED)
    if start and end:
        since, until = _range(start, end)
        expenses = expenses.filter(decided_at__gte=since, decided_at__lt=until)
    return expenses


def summary(start: date, end: date) -> dict:
    """Income by heading, money held for members, and expenditure, for the days from `start` to `end`."""
    payments = _payments(start, end)
    rows = (
        payments.filter(status__in=COUNTED).values("reason").annotate(gross=Sum("amount_pence"), fees=Sum("fee_pence"))
    )
    by_reason = {row["reason"]: row for row in rows}
    headings = []
    for key, label, reasons in HEADINGS:
        lines = [
            {
                "reason": reason,
                "label": REASON_LABELS[reason],
                "gross": by_reason[reason]["gross"] or 0,
                "fees": by_reason[reason]["fees"] or 0,
            }
            for reason in reasons
            if reason in by_reason
        ]
        gross, fees = sum(line["gross"] for line in lines), sum(line["fees"] for line in lines)
        headings.append({"key": key, "label": label, "lines": lines, "gross": gross, "fees": fees, "net": gross - fees})
    gross = sum(h["gross"] for h in headings)
    fees = sum(h["fees"] for h in headings)
    top_ups = payments.filter(status__in=COUNTED, reason=R.BALANCE)
    expenses = _approved_expenses(start, end)
    by_category = expenses.values("category").annotate(total=Sum("amount_pence")).order_by("-total")
    categories = dict(Expense.Category.choices)
    spent = sum(row["total"] for row in by_category)
    return {
        "headings": headings,
        "gross": gross,
        "fees": fees,
        "net": gross - fees,
        "pending": payments.filter(status=Payment.Status.PENDING)
        .exclude(reason__in=(R.BALANCE, R.WITHDRAWAL))
        .aggregate(total=Sum("amount_pence"))["total"]
        or 0,
        "credit_added": top_ups.exclude(source=Payment.Source.REIMBURSEMENT).aggregate(t=Sum("amount_pence"))["t"] or 0,
        "credit_fees": top_ups.aggregate(t=Sum("fee_pence"))["t"] or 0,
        "credit_paid_out": payments.filter(status__in=COUNTED, reason=R.WITHDRAWAL).aggregate(t=Sum("amount_pence"))[
            "t"
        ]
        or 0,
        "expenses": [{"label": categories.get(row["category"], row["category"]), **row} for row in by_category],
        "spent": spent,
        "after_expenses": gross - fees - spent,
    }


def months(last: date, count=12) -> list[dict]:
    """Income by heading and expenses, month by month, for the `count` months up to `last`'s month."""
    first = last.replace(day=1)
    for _ in range(count - 1):
        first = (first - timedelta(days=1)).replace(day=1)
    end = (last.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    totals = defaultdict(lambda: defaultdict(int))
    rows = (
        _payments(first, end)
        .filter(status__in=COUNTED, reason__in=HEADING_OF)
        .annotate(month=TruncMonth("created_at"))
        .values("month", "reason")
        .annotate(net=Sum("amount_pence") - Sum("fee_pence"))
    )
    for row in rows:
        totals[timezone.localtime(row["month"]).date() if isinstance(row["month"], datetime) else row["month"]][
            HEADING_OF[row["reason"]]
        ] += row["net"]
    spending = (
        _approved_expenses(first, end)
        .annotate(month=TruncMonth("decided_at"))
        .values("month")
        .annotate(t=Sum("amount_pence"))
    )
    for row in spending:
        month = timezone.localtime(row["month"]).date() if isinstance(row["month"], datetime) else row["month"]
        totals[month]["spent"] += row["t"]
    result, month = [], first
    while month <= end:
        values = totals.get(month, {})
        income = sum(values.get(key, 0) for key, _, _ in HEADINGS)
        result.append(
            {
                "month": month,
                **{k: values.get(k, 0) for k, _, _ in HEADINGS},
                "income": income,
                "spent": values.get("spent", 0),
            }
        )
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return result


def machines(start: date | None = None, end: date | None = None) -> dict:
    """What each machine (or group of machines sharing an induction) has made."""
    payments = Payment.objects.filter(status__in=COUNTED)
    if start and end:
        since, until = _range(start, end)
        payments = payments.filter(created_at__gte=since, created_at__lt=until)
    inductions = dict(
        payments.filter(reason=R.INDUCTION).values_list("reference").annotate(t=Sum("amount_pence")).order_by()
    )
    usage = dict(
        payments.filter(reason=R.EQUIPMENT_FEE, usage_session__isnull=False)
        .values_list("usage_session__equipment")
        .annotate(t=Sum("amount_pence"))
        .order_by()
    )
    unlinked_usage = (
        payments.filter(reason=R.EQUIPMENT_FEE, usage_session__isnull=True).aggregate(t=Sum("amount_pence"))["t"] or 0
    )
    expenses = _approved_expenses(start, end)
    spent = dict(
        expenses.filter(equipment__isnull=False).values_list("equipment").annotate(t=Sum("amount_pence")).order_by()
    )
    groups = {}
    for machine in Equipment.objects.order_by("name"):
        key = machine.induction_key if machine.requires_induction else f"machine:{machine.pk}"
        group = groups.setdefault(key, {"machines": [], "induction": 0, "usage": 0, "expenses": 0})
        group["machines"].append(machine)
        group["usage"] += usage.get(machine.pk, 0)
        group["expenses"] += spent.get(machine.pk, 0)
        if machine.requires_induction:
            group["induction"] = inductions.get(machine.induction_key, 0)
    known = {key for key in groups}
    other_inductions = {ref: total for ref, total in inductions.items() if ref not in known}
    rows = []
    for group in groups.values():
        group["net"] = group["induction"] + group["usage"] - group["expenses"]
        group["archived"] = all(m.archived for m in group["machines"])
        if group["archived"] and not (group["induction"] or group["usage"] or group["expenses"]):
            continue
        rows.append(group)
    rows.sort(key=lambda g: g["net"], reverse=True)
    return {
        "rows": rows,
        "induction": sum(g["induction"] for g in rows),
        "usage": sum(g["usage"] for g in rows),
        "expenses": sum(g["expenses"] for g in rows),
        "net": sum(g["net"] for g in rows),
        "other_inductions": sorted(other_inductions.items(), key=lambda item: -item[1]),
        "unlinked_usage": unlinked_usage,
        "unlinked_expenses": expenses.filter(equipment__isnull=True).aggregate(t=Sum("amount_pence"))["t"] or 0,
    }


# -- Money received, by source (04 §4.9, "Reconciliation with the bank") ------------------------------------------

RECEIVED = [
    # key, label, the bank statement's kind of line, what BBMS knows
    ("gocardless", "GoCardless payouts", "gocardless", "payouts"),
    ("stripe", "Stripe payouts", "stripe", "payouts"),
    ("member", "Standing orders and transfers", "member", "standing-order"),
    ("paypal", "PayPal transfers", "paypal", None),
    ("other", "Other money in", "other", None),
    ("cash", "Cash", None, "cash"),
]


def received(start: date, end: date) -> dict:
    """What reached the bank from each source, by the day it arrived, as BBMS knows it and as the bank statements say,
    so the two can be compared line by line. Payouts are dated by when they arrive at the bank, not when members
    paid."""
    from .models import BankLine, Payout

    payouts = Payout.objects.filter(arrival_date__gte=start, arrival_date__lte=end).exclude(
        status__in=("bounced", "failed", "canceled")
    )
    lines = BankLine.objects.filter(kept=True, date__gte=start, date__lte=end)
    since, until = _range(start, end)
    paid = Payment.objects.filter(status__in=COUNTED, paid_at__gte=since, paid_at__lt=until)
    rows = []
    for key, label, kind, known in RECEIVED:
        bank = lines.filter(kind=kind).aggregate(t=Sum("amount_pence"))["t"] or 0 if kind else None
        if known == "payouts":
            ours = payouts.filter(provider=key).aggregate(t=Sum("amount_pence"))["t"] or 0
        elif known == "standing-order":
            ours = paid.filter(source=Payment.Source.STANDING_ORDER).aggregate(t=Sum("amount_pence"))["t"] or 0
        elif known == "cash":
            ours = (
                paid.filter(source=Payment.Source.CASH, amount_pence__gt=0).aggregate(t=Sum("amount_pence"))["t"] or 0
            )
        else:
            ours = None
        difference = bank - ours if bank is not None and ours is not None else None
        rows.append({"key": key, "label": label, "bbms": ours, "bank": bank, "difference": difference})
    has_statement = lines.exists()
    return {
        "rows": rows,
        "bank_total": sum(row["bank"] or 0 for row in rows),
        "has_statement": has_statement,
        "unmatched_lines": lines.filter(outcome=BankLine.Outcome.UNMATCHED).order_by("date"),
        "payout_lines_alone": lines.filter(
            kind__in=(BankLine.Kind.GOCARDLESS, BankLine.Kind.STRIPE), payout_match__isnull=True
        ).order_by("date"),
        # Only worth listing once a statement for the period has been imported.
        "payouts_not_on_statement": payouts.filter(bank_line__isnull=True, status="paid").order_by("arrival_date")
        if has_statement
        else Payout.objects.none(),
    }
