"""
GoCardless and Stripe payouts (spec 04 §4.9, "Reconciliation with the bank").

Both providers pay the bank in lump sums: the payments of the last few days, less their fees. BBMS imports each payout
and what's inside it from the provider's API, so that:

- each member payment gets the provider's **real fee** (legacy recorded GoCardless fees as £0 and guessed Stripe's);
- each GoCardless or Stripe line on a bank statement can be **matched** to the payout it is, and through it to the
  payments it is made of;
- payments inside a payout that BBMS doesn't know about are listed rather than hidden.

Importing is safe to repeat: a payout is stored once, its status kept up to date, and its items stored once it is
paid. The fee is the one thing changed on an existing payment: it isn't money owed by or to the member, so it doesn't
touch their credit or the ledger's amounts.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.billing import gocardless
from apps.payments import cards
from apps.payments.models import Payment

from .models import BankLine, Payout, PayoutItem

logger = logging.getLogger(__name__)

FIRST_IMPORT_DAYS = 120  # how far back the first import looks
OVERLAP_DAYS = 14  # later imports look again this far before the newest payout, to catch status changes
MATCH_DAYS = 4  # a bank line can be this many days from the payout's arrival date (weekends, bank holidays)

SOURCE = {Payout.Provider.GOCARDLESS: Payment.Source.GOCARDLESS, Payout.Provider.STRIPE: Payment.Source.STRIPE}
LINE_KIND = {Payout.Provider.GOCARDLESS: BankLine.Kind.GOCARDLESS, Payout.Provider.STRIPE: BankLine.Kind.STRIPE}


def _client(provider):
    if provider == Payout.Provider.GOCARDLESS:
        return gocardless.client(), gocardless.GoCardlessError
    return cards.client(), cards.CardError


@dataclass
class Summary:
    new: int = 0
    updated: int = 0
    fees_set: int = 0
    unknown_payments: int = 0
    matched_lines: int = 0
    errors: list = field(default_factory=list)

    def __str__(self):
        parts = [f"{self.new} new payout(s)", f"{self.updated} updated", f"{self.fees_set} fee(s) recorded"]
        if self.unknown_payments:
            parts.append(f"{self.unknown_payments} payment(s) BBMS doesn't know")
        parts.append(f"{self.matched_lines} bank line(s) matched")
        return ", ".join(parts)


def default_since(provider):
    newest = Payout.objects.filter(provider=provider).order_by("-arrival_date").first()
    if newest is None:
        return timezone.localdate() - timedelta(days=FIRST_IMPORT_DAYS)
    return newest.arrival_date - timedelta(days=OVERLAP_DAYS)


def import_payouts(since=None, providers=None) -> Summary:
    """Fetch payouts from each provider and store them; then match bank lines. One provider failing doesn't stop
    the other: its error is in the summary."""
    summary = Summary()
    for provider in providers or Payout.Provider.values:
        api, error_class = _client(provider)
        start = since or default_since(provider)
        try:
            found = api.payouts(start)
        except error_class as error:
            summary.errors.append(f"{Payout.Provider(provider).label}: {error}")
            continue
        for data in found:
            _store(provider, data, summary)
    summary.matched_lines = match_bank_lines()
    return summary


@transaction.atomic
def _store(provider, data, summary):
    payout, created = Payout.objects.select_for_update().get_or_create(
        provider=provider,
        provider_id=data["id"],
        defaults={
            "amount_pence": data["amount_pence"],
            "fees_pence": data["fees_pence"],
            "arrival_date": data["arrival_date"],
            "status": data["status"],
            "reference": data["reference"][:100],
        },
    )
    if not created:
        changed = (payout.status, payout.amount_pence, payout.arrival_date) != (
            data["status"],
            data["amount_pence"],
            data["arrival_date"],
        )
        if changed:
            payout.status, payout.amount_pence = data["status"], data["amount_pence"]
            payout.fees_pence, payout.arrival_date = data["fees_pence"], data["arrival_date"]
            payout.save(update_fields=["status", "amount_pence", "fees_pence", "arrival_date"])
            summary.updated += 1
    else:
        summary.new += 1
    if payout.items.exists():
        return
    for item in data["items"]:
        payment = None
        if item["payment_id"]:
            payment = (
                Payment.objects.filter(source=SOURCE[provider], source_id=item["payment_id"], corrects__isnull=True)
                .order_by("pk")
                .first()
            )
        PayoutItem.objects.create(
            payout=payout,
            kind=item["kind"],
            amount_pence=item["amount_pence"],
            fee_pence=item["fee_pence"],
            provider_payment_id=item["payment_id"][:100],
            description=item["description"][:255],
            payment=payment,
        )
        if item["kind"] != PayoutItem.Kind.PAYMENT:
            continue
        if payment is None:
            summary.unknown_payments += 1
        elif payment.fee_pence != item["fee_pence"]:
            Payment.objects.filter(pk=payment.pk).update(fee_pence=item["fee_pence"])
            summary.fees_set += 1


def match_bank_lines() -> int:
    """Link each recorded GoCardless or Stripe bank line to its payout: the same amount, arriving within a few days,
    and not matched to another line. Where several could fit, the payout whose reference the line mentions, then the
    nearest date, wins."""
    matched = 0
    lines = BankLine.objects.filter(
        kept=True, outcome=BankLine.Outcome.PAYOUT, kind__in=LINE_KIND.values(), payout_match__isnull=True
    ).order_by("date", "pk")
    for line in lines:
        provider = next(p for p, kind in LINE_KIND.items() if kind == line.kind)
        candidates = Payout.objects.filter(
            provider=provider,
            amount_pence=line.amount_pence,
            bank_line__isnull=True,
            arrival_date__gte=line.date - timedelta(days=MATCH_DAYS),
            arrival_date__lte=line.date + timedelta(days=MATCH_DAYS),
        ).exclude(status__in=("bounced", "failed", "canceled"))
        best = min(candidates, key=lambda payout, line=line: _closeness(payout, line), default=None)
        if best is not None:
            best.bank_line = line
            best.save(update_fields=["bank_line"])
            matched += 1
    return matched


def _closeness(payout, line):
    mentioned = bool(payout.reference) and payout.reference.upper() in line.description.upper()
    return (not mentioned, abs((payout.arrival_date - line.date).days), payout.pk)


def unmatch(payout):
    payout.bank_line = None
    payout.save(update_fields=["bank_line"])
