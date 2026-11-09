"""
Reading a bank statement and recording the payments in it (spec 04 §4.6, §4.9).

**Reading.** The statement is the bank's monthly CSV. Legacy's layout is read by position (date, type, description,
-, amount; credits have the type `CR`). A file with a header row is read by its column names instead (a "Date",
a "Description" and a "Paid in" / "Credit" or signed "Amount" column), so a change of bank only needs a new export.
Only money coming in is kept.

**Sorting.** GoCardless and Stripe payouts and PayPal transfers are lumps made of payments BBMS already has, so they
aren't recorded again; Eventbrite money isn't from members. Everything else is taken to be from a member.

**Matching**, in this order, and only if it points at exactly one member:
1. a description finance has confirmed before (remembered when they record it);
2. a piece of text finance has said identifies a member (legacy's "import match string");
3. the member's family name as a whole word in the description, narrowed by first name or initial if several
   members share it. Members who have left aren't matched by name.
Legacy matched any family name anywhere in the description (so "LEE" matched "FLEET"), and saved straight away.
Here everything is a suggestion: finance checks each line before anything is recorded.

**Recording.** A subscription pays the member's oldest open subscription charge, dated the day the money reached
the bank (so their membership runs to a month after that); with no open charge it goes to their credit instead. For
standing-order members the monthly amount becomes what they paid, as in legacy. A line already recorded from an
earlier statement is spotted and left out.
"""

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from apps.billing import membership
from apps.billing.models import Charge
from apps.members.models import Member
from apps.payments import services
from apps.payments.models import Payment

from .models import BankLine, BankMatch, BankStatement

logger = logging.getLogger(__name__)

MAX_BYTES = 2 * 1024 * 1024
DATE_FORMATS = (
    "%d/%m/%Y",
    "%d/%m/%y",
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%d-%b-%y",
    "%d-%b-%Y",
    "%d %b %y",
)
PAYOUTS = (
    ("GC C1 BUILDBRIGHTO", BankLine.Kind.GOCARDLESS),
    ("GOCARDLESS", BankLine.Kind.GOCARDLESS),
    ("STRIPE", BankLine.Kind.STRIPE),
    ("PAYPAL", BankLine.Kind.PAYPAL),
    ("EVENTBRITE", BankLine.Kind.OTHER),
)
NOT_FROM_MEMBERS = {BankLine.Kind.GOCARDLESS, BankLine.Kind.STRIPE, BankLine.Kind.PAYPAL, BankLine.Kind.OTHER}


class BadStatement(Exception):
    """The file couldn't be read as a statement. The message is shown to finance."""


class CantRecord(Exception):
    """A line couldn't be recorded as asked. The message is shown to finance."""


def tidy(text) -> str:
    """Capitals, with runs of spaces made one: how descriptions are compared."""
    return " ".join(str(text).upper().split())


# -- Reading ------------------------------------------------------------------------------------------------------


@dataclass
class Credit:
    row: int
    date: date
    description: str
    amount_pence: int


def _date(text):
    text = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _pence(text):
    text = str(text).strip().replace("£", "").replace(",", "").replace(" ", "")
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    try:
        value = Decimal(text.strip("()"))
    except InvalidOperation:
        return None
    pence = int((value * 100).quantize(Decimal("1")))
    return -pence if negative else pence


def _column(header, *names, contains=()):
    for name in names:
        if name in header:
            return header.index(name)
    for index, cell in enumerate(header):
        if any(word in cell for word in contains):
            return index
    return None


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise BadStatement("That file isn't a text (CSV) file.")


def read_credits(upload) -> tuple[list[Credit], int]:
    """The credits in an uploaded CSV statement, and how many other lines were skipped. Raises BadStatement."""
    if upload.size > MAX_BYTES:
        raise BadStatement("That file is too big for a monthly statement.")
    text = _decode(upload.read())
    if "\x00" in text:
        raise BadStatement("That doesn't look like a CSV file. Please save the statement as CSV.")
    rows = [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]
    if not rows:
        raise BadStatement("That file is empty.")
    header = [tidy(cell).lower() for cell in rows[0]]
    if any("date" in cell for cell in header) and _date(rows[0][0]) is None:
        columns = {
            "date": _column(header, "date", "transaction date", "posting date", contains=("date",)),
            "type": _column(header, "type", "transaction type", contains=("type",)),
            "description": _column(
                header,
                "description",
                "transaction description",
                "details",
                "narrative",
                "memo",
                contains=("description", "details", "narrative", "reference", "payee", "name"),
            ),
            "credit": _column(header, "paid in", "credit amount", "credit", "money in", "in", contains=("paid in",)),
            "amount": _column(header, "amount", "value", contains=("amount",)),
        }
        if (
            columns["date"] is None
            or columns["description"] is None
            or (columns["credit"] is None and columns["amount"] is None)
        ):
            raise BadStatement("The header row needs a date, a description and a paid-in (or amount) column.")
        body, first = rows[1:], 2
    else:
        columns, body, first = {"date": 0, "type": 1, "description": 2, "credit": None, "amount": 4}, rows, 1
        if len(rows[0]) < 5:
            raise BadStatement("The statement should have columns for date, type, description and amount.")
    credits, skipped = [], 0
    latest = timezone.localdate() + timedelta(days=1)
    for number, row in enumerate(body, start=first):

        def cell(name, row=row):
            index = columns[name]
            return row[index].strip() if index is not None and index < len(row) else ""

        when = _date(cell("date"))
        if when is None:
            skipped += 1
            continue
        if when > latest:
            raise BadStatement(f"Row {number} is dated {when:%d %b %Y}, in the future. Is it a UK-style date?")
        if columns["credit"] is not None:
            amount = _pence(cell("credit"))
            is_credit = bool(amount and amount > 0)
        else:
            amount = _pence(cell("amount"))
            kind = cell("type").upper() if columns["type"] is not None else ""
            if first == 1:  # legacy's layout: the type says it's a credit, and the amount isn't signed
                is_credit, amount = kind == "CR", abs(amount or 0)
            else:
                is_credit = bool(amount and amount > 0) and kind not in ("DR", "DEBIT", "D")
        if not is_credit or not amount:
            skipped += 1
            continue
        description = cell("description")[:500]
        credits.append(Credit(number, when, description, amount))
    if not credits:
        raise BadStatement("No money coming in was found in that file. Is it the right statement?")
    return credits, skipped


# -- Sorting and matching -----------------------------------------------------------------------------------------


def kind_of(description) -> str:
    text = tidy(description)
    for words, kind in PAYOUTS:
        if words in text:
            return kind
    return BankLine.Kind.MEMBER


def reason_of(description) -> tuple[str, str]:
    """(suggested reason, reference) from the description, as legacy read it."""
    text = str(description)
    if "-BALANCE-" in text.upper():
        return BankLine.Reason.BALANCE, text.upper().split("-BALANCE-", 1)[1].strip().lower()[:100]
    if "SUB" in tidy(text) or "MEMBERSHIP" in tidy(text):
        return BankLine.Reason.SUBSCRIPTION, ""
    return "", ""


def _words(text) -> str:
    return " " + re.sub(r"[^A-Z0-9]+", " ", tidy(text)).strip() + " "


@dataclass
class Match:
    member: Member | None = None
    how: str = ""
    candidates: list = field(default_factory=list)


class Matcher:
    """Finds the member a description belongs to. Built once per statement."""

    def __init__(self):
        self.whole = {m.text: m.member for m in BankMatch.objects.filter(whole=True).select_related("member")}
        self.pieces = [(m.text, m.member) for m in BankMatch.objects.filter(whole=False).select_related("member")]
        self.by_name = {}
        for member in Member.objects.exclude(status=Member.Status.LEFT).exclude(family_name=""):
            self.by_name.setdefault(_words(member.family_name), []).append(member)

    def match(self, description) -> Match:
        text = tidy(description)
        if text in self.whole:
            return Match(self.whole[text], "Recorded for them before")
        found = {member.pk: (piece, member) for piece, member in self.pieces if piece in text}
        if len(found) == 1:
            piece, member = next(iter(found.values()))
            return Match(member, f"Contains “{piece}”")
        if found:
            return Match(None, "Matches text set for several members", [m for _, m in found.values()])
        words = _words(description)
        named = [m for name, members in self.by_name.items() if name in words for m in members]
        if len(named) > 1:
            first = [m for m in named if _words(m.given_name).strip() and _words(m.given_name) in words]
            initial = [m for m in named if m.given_name and f" {m.given_name[0].upper()} " in words]
            named = first if len(first) == 1 else initial if len(initial) == 1 else named
            if len(named) == 1:
                return Match(named[0], "Name")
            return Match(None, "Several members have this family name", named[:6])
        if named:
            return Match(named[0], f"Family name “{named[0].family_name}”")
        return Match()


def fingerprint(credit, seen) -> str:
    key = f"{credit.date:%Y-%m-%d}|{credit.amount_pence}|{tidy(credit.description)}"
    seen[key] = seen.get(key, 0) + 1
    return f"{key}|{seen[key]}"


@transaction.atomic
def read_statement(upload, by) -> BankStatement:
    """Read an uploaded statement into a draft with a suggestion for each line. Raises BadStatement."""
    credits, skipped = read_credits(upload)
    statement = BankStatement.objects.create(filename=upload.name[:255], uploaded_by=by, skipped_lines=skipped)
    matcher, seen = Matcher(), {}
    lines = []
    for credit in credits:
        kind = kind_of(credit.description)
        reason, reference = reason_of(credit.description)
        line = BankLine(
            statement=statement,
            row=credit.row,
            date=credit.date,
            description=credit.description,
            amount_pence=credit.amount_pence,
            kind=kind,
            fingerprint=fingerprint(credit, seen),
            suggested_reason=reason if kind == BankLine.Kind.MEMBER else "",
            reference=reference,
        )
        if kind == BankLine.Kind.MEMBER:
            found = matcher.match(credit.description)
            line.suggested_member, line.how_matched = found.member, found.how
            if found.candidates:
                line.how_matched += ": " + ", ".join(m.full_name for m in found.candidates)
        lines.append(line)
    recorded_before = set(
        BankLine.objects.filter(kept=True, fingerprint__in=[line.fingerprint for line in lines]).values_list(
            "fingerprint", flat=True
        )
    )
    for line in lines:
        if line.fingerprint in recorded_before:
            line.outcome = BankLine.Outcome.DUPLICATE
    BankLine.objects.bulk_create(lines)
    logger.info("Statement %s read: %s credits, %s other lines", statement.pk, len(lines), skipped)
    return statement


# -- Deciding and recording ---------------------------------------------------------------------------------------

SUBSCRIPTION, BALANCE, NOT_MEMBER, UNMATCHED, PAYOUT = "subscription", "balance", "not-member", "unmatched", "payout"
ACTIONS = {
    SUBSCRIPTION: "Subscription",
    BALANCE: "Credit top-up",
    UNMATCHED: "Leave unmatched",
    NOT_MEMBER: "Not a member payment",
    PAYOUT: "Payout (already in BBMS)",
}


def default_action(line) -> str:
    """What the review page suggests for a line."""
    if line.kind in (BankLine.Kind.GOCARDLESS, BankLine.Kind.STRIPE, BankLine.Kind.PAYPAL):
        return PAYOUT
    if line.kind == BankLine.Kind.OTHER:
        return NOT_MEMBER
    member = line.suggested_member
    if member is None:
        return UNMATCHED
    if line.suggested_reason:
        return line.suggested_reason
    return SUBSCRIPTION if member.payment_method == Member.PaymentMethod.STANDING_ORDER else BALANCE


def actions_for(line) -> list[tuple[str, str]]:
    names = [SUBSCRIPTION, BALANCE, UNMATCHED, NOT_MEMBER]
    if line.kind in (BankLine.Kind.GOCARDLESS, BankLine.Kind.STRIPE, BankLine.Kind.PAYPAL):
        names = [PAYOUT, *names]
    return [(name, ACTIONS[name]) for name in names]


def _when(day) -> datetime:
    return timezone.make_aware(datetime.combine(day, time(12)))


def _remember(line, member, by):
    """Next time the same description is matched straight to this member."""
    text = tidy(line.description)
    if not text:
        return
    BankMatch.objects.update_or_create(text=text, defaults={"member": member, "whole": True, "created_by": by})


def _pay(line, member, action, by) -> tuple[Payment, str]:
    note = f"From the bank statement: {line.description}"
    fields = {"created_at": _when(line.date), "paid_at": _when(line.date), "created_by": by, "note": note}
    if action == SUBSCRIPTION:
        member = Member.objects.select_for_update().get(pk=member.pk)
        charge = (
            member.charges.select_for_update()
            .filter(kind=Charge.Kind.SUBSCRIPTION, status__in=(Charge.Status.PENDING, *Charge.UNPAID))
            .order_by("charge_date")
            .first()
        )
        if charge is not None:
            charge.amount_pence = line.amount_pence
            charge.save(update_fields=["amount_pence"])
            payment = services.record_payment(
                member,
                Payment.Reason.SUBSCRIPTION,
                Payment.Source.STANDING_ORDER,
                line.amount_pence,
                charge=charge,
                reference=f"charge {charge.pk}",
                **fields,
            )
            membership.charge_paid(charge, line.date)
            if member.payment_method == Member.PaymentMethod.STANDING_ORDER:
                member.monthly_subscription_pence = line.amount_pence
                member.save(update_fields=["monthly_subscription_pence"])
            return payment, f"Paid their subscription due {charge.charge_date:%d %b %Y}."
        outcome = "No subscription was waiting to be paid, so it was added to their credit."
    else:
        outcome = "Added to their credit."
    payment = services.record_payment(
        member,
        Payment.Reason.BALANCE,
        Payment.Source.STANDING_ORDER,
        line.amount_pence,
        reference=line.reference or "bank statement",
        **fields,
    )
    return payment, outcome


def decide(line, action, member, by, *, remember=True):
    """Carry out finance's decision for one line (already locked, inside a transaction). Raises CantRecord."""
    if action not in ACTIONS:
        raise CantRecord("Please choose what this line is.")
    if action == PAYOUT and line.kind == BankLine.Kind.MEMBER:
        raise CantRecord("Only a GoCardless, Stripe or PayPal line is a payout.")
    if action in (SUBSCRIPTION, BALANCE):
        if member is None:
            raise CantRecord("Please choose the member who paid.")
        line.payment, line.note = _pay(line, member, action, by)
        line.member, line.outcome = member, BankLine.Outcome.RECORDED
        if remember and line.kind == BankLine.Kind.MEMBER:
            _remember(line, member, by)
    else:
        line.outcome = {
            PAYOUT: BankLine.Outcome.PAYOUT,
            NOT_MEMBER: BankLine.Outcome.NOT_MEMBER,
            UNMATCHED: BankLine.Outcome.UNMATCHED,
        }[action]
        line.member = None
    line.handled_by, line.handled_at = by, timezone.now()
    line.kept = True
    line.save()


@transaction.atomic
def record(statement, decisions, by) -> dict:
    """Record a draft statement: `decisions` is {line id: (action, member or None)}, for every line still to do.

    All or nothing: if any line can't be recorded, nothing is, and CantRecord says which.
    """
    statement = BankStatement.objects.select_for_update().get(pk=statement.pk)
    if not statement.is_draft:
        raise CantRecord("This statement has already been recorded.")
    counts = {}
    lines = statement.lines.select_for_update().exclude(outcome=BankLine.Outcome.DUPLICATE)
    # Another statement recorded meanwhile may have included the same lines.
    taken = set(
        BankLine.objects.filter(kept=True, fingerprint__in=lines.values("fingerprint")).values_list(
            "fingerprint", flat=True
        )
    )
    for line in lines.order_by("date", "row"):
        if line.fingerprint in taken:
            line.outcome = BankLine.Outcome.DUPLICATE
            line.save(update_fields=["outcome"])
            continue
        action, member = decisions.get(line.pk, (None, None))
        try:
            decide(line, action, member, by)
        except CantRecord as error:
            raise CantRecord(f"Row {line.row} ({line.description}): {error}") from error
        counts[line.outcome] = counts.get(line.outcome, 0) + 1
    statement.recorded_at, statement.recorded_by = timezone.now(), by
    statement.save(update_fields=["recorded_at", "recorded_by"])
    from .payouts import match_bank_lines  # payouts imported earlier can now be matched to these lines (04 §4.9)

    counts["payouts matched"] = match_bank_lines()
    logger.info("Statement %s recorded by member %s: %s", statement.pk, by.pk, counts)
    return counts


@transaction.atomic
def match_later(line, action, member, by):
    """Finance matches a line they left unmatched (or said wasn't a member's) on an earlier statement."""
    line = BankLine.objects.select_for_update().get(pk=line.pk)
    if line.outcome not in (BankLine.Outcome.UNMATCHED, BankLine.Outcome.NOT_MEMBER) or not line.kept:
        raise CantRecord("Only an unmatched line can be matched.")
    decide(line, action, member, by)
    return line


def unmatched_lines():
    return BankLine.objects.filter(kept=True, outcome=BankLine.Outcome.UNMATCHED)
