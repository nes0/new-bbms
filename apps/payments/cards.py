"""
Talking to Stripe, for card payments (spec 04 §4.3).

`client()` returns one of:
- `StripeCards`: the real API (the official `stripe` library), when `STRIPE_SECRET_KEY` is set. Use a **test mode**
  key (`sk_test_…`) everywhere except the live site.
- `FakeCards`: when developing or testing without a key. Its "checkout page" is a local page (`/dev/stripe/…`, DEBUG
  only) with a "Pay" button. When developing, its state is kept in a file so every process sees it.
- `NotConfigured`: a live site without a key. Every call fails politely ("card payments aren't available").

Members pay on Stripe's own Checkout page, so card details never reach BBMS.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time

from django.conf import settings

from apps.core import fakes

logger = logging.getLogger(__name__)


class CardError(Exception):
    """Stripe refused or couldn't be reached. The message is safe to show a member."""


@dataclass
class Checkout:
    id: str
    url: str


@dataclass
class Result:
    """What Stripe says about a checkout session."""

    paid: bool
    payment_id: str = ""  # the PaymentIntent
    amount_pence: int = 0
    fee_pence: int | None = None  # Stripe's real fee, when its balance transaction is ready
    expired: bool = False


def _line(amount_pence, description):
    return {
        "quantity": 1,
        "price_data": {"currency": "gbp", "unit_amount": amount_pence, "product_data": {"name": description}},
    }


class StripeCards:
    """The real API."""

    def __init__(self, secret_key):
        import stripe

        self._stripe = stripe
        self._api = stripe.StripeClient(secret_key)

    def _call(self, what, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except self._stripe.StripeError as error:
            logger.warning("Stripe refused to %s: %s", what, error)
            raise CardError(f"Stripe couldn't {what}. Please try again later.") from error

    def create_checkout(self, *, amount_pence, description, email, reference, success_url, cancel_url) -> Checkout:
        session = self._call(
            "start a card payment",
            self._api.v1.checkout.sessions.create,
            params={
                "mode": "payment",
                "payment_method_types": ["card"],
                "line_items": [_line(amount_pence, description)],
                "customer_email": email,
                "client_reference_id": reference,
                "metadata": {"checkout": reference},
                "success_url": success_url + "?session_id={CHECKOUT_SESSION_ID}",
                "cancel_url": cancel_url,
            },
            options={"idempotency_key": f"bbms-checkout-{reference}"},
        )
        return Checkout(session.id, session.url)

    def result(self, session_id) -> Result:
        session = self._call(
            "check a card payment",
            self._api.v1.checkout.sessions.retrieve,
            session_id,
            params={"expand": ["payment_intent.latest_charge.balance_transaction"]},
        )
        if session.payment_status != "paid":
            return Result(paid=False, expired=session.status == "expired")
        intent = session.payment_intent
        fee = None
        charge = getattr(intent, "latest_charge", None)
        balance = getattr(charge, "balance_transaction", None) if charge else None
        if balance is not None and not isinstance(balance, str):
            fee = balance.fee
        return Result(paid=True, payment_id=intent.id, amount_pence=session.amount_total, fee_pence=fee)

    def _all(self, what, listing):
        try:
            return list(listing.auto_paging_iter())
        except self._stripe.StripeError as error:
            logger.warning("Stripe refused to %s: %s", what, error)
            raise CardError(f"Stripe couldn't {what}. Please try again later.") from error

    def payouts(self, since: date) -> list[dict]:
        """Payouts created since `since`, each with what's inside it (spec 04 §4.9). The shape is the same as
        `apps.billing.gocardless.payout_dict`."""
        created = int(datetime.combine(since, time.min, tzinfo=UTC).timestamp())
        listing = self._call(
            "list payouts", self._api.v1.payouts.list, params={"created": {"gte": created}, "limit": 100}
        )
        found = []
        for payout in self._all("list payouts", listing):
            if payout.currency != "gbp":
                continue
            transactions = self._call(
                "list a payout's payments",
                self._api.v1.balance_transactions.list,
                params={"payout": payout.id, "limit": 100, "expand": ["data.source"]},
            )
            items = [stripe_item(t) for t in self._all("list a payout's payments", transactions) if t.type != "payout"]
            found.append(
                {
                    "id": payout.id,
                    "amount_pence": payout.amount,
                    "fees_pence": sum(item["fee_pence"] for item in items),
                    "arrival_date": datetime.fromtimestamp(payout.arrival_date, UTC).date(),
                    "status": payout.status,
                    "reference": payout.statement_descriptor or "",
                    "items": items,
                }
            )
        return found


def stripe_item(transaction) -> dict:
    """One balance transaction inside a payout, as an item. A card payment's id is its PaymentIntent, which is what
    BBMS stored when the member paid."""
    payment_id = ""
    if transaction.type in ("charge", "payment"):
        source = transaction.source
        intent = getattr(source, "payment_intent", "") if source is not None and not isinstance(source, str) else ""
        payment_id = intent if isinstance(intent, str) else getattr(intent, "id", "")
        kind = "payment"
    elif transaction.type in ("refund", "payment_refund", "payment_failure_refund", "adjustment"):
        kind = "refund"
    elif transaction.type == "stripe_fee":
        kind = "fee"
    else:
        kind = "other"
    return {
        "kind": kind,
        "amount_pence": transaction.amount,
        "fee_pence": transaction.fee,
        "payment_id": payment_id or "",
        "description": transaction.description or transaction.type,
    }


@dataclass
class FakeCards:
    """Behaves like Stripe Checkout for local development and tests. Nothing leaves the machine."""

    path: str | None = None
    sessions: dict = field(default_factory=dict)  # id -> {"amount", "description", "success_url", "cancel_url", ...}
    payouts_made: list = field(default_factory=list)  # payout dicts, with the date as text
    next_id: int = 1
    fail_next: str = ""

    STATE = ("sessions", "payouts_made", "next_id")
    FEE_PERCENT, FEE_FIXED = 1.5, 20  # a typical UK card fee, so pages show something realistic

    def load(self):
        if self.path and os.path.exists(self.path):
            with open(self.path) as file:
                for name, value in json.load(file).items():
                    setattr(self, name, value)
        return self

    def _save(self):
        if self.path:
            with open(self.path, "w") as file:
                json.dump({name: getattr(self, name) for name in self.STATE}, file, indent=1)

    def create_checkout(self, *, amount_pence, description, email, reference, success_url, cancel_url):
        if self.fail_next:
            message, self.fail_next = self.fail_next, ""
            raise CardError(message)
        session_id = f"cs_test_fake{self.next_id:06d}"
        self.next_id += 1
        self.sessions[session_id] = {
            "amount": amount_pence,
            "description": description,
            "reference": reference,
            "success_url": f"{success_url}?session_id={session_id}",
            "cancel_url": cancel_url,
            "status": "open",
        }
        self._save()
        return Checkout(session_id, f"/dev/stripe/{session_id}/")

    def pay(self, session_id) -> str:
        """What the member does on Stripe's page. Returns where Stripe sends them back to."""
        session = self.sessions[session_id]
        session["status"] = "paid"
        self._save()
        return session["success_url"]

    def expire(self, session_id):
        self.sessions[session_id]["status"] = "expired"
        self._save()

    def result(self, session_id) -> Result:
        session = self.sessions.get(session_id)
        if session is None:
            raise CardError("Stripe couldn't check a card payment. Please try again later.")
        if session["status"] != "paid":
            return Result(paid=False, expired=session["status"] == "expired")
        amount = session["amount"]
        return Result(
            paid=True,
            payment_id=f"pi_fake_{session_id[-6:]}",
            amount_pence=amount,
            fee_pence=self._fee(amount),
        )

    def _fee(self, amount):
        return round(amount * self.FEE_PERCENT / 100) + self.FEE_FIXED

    def add_payout(self, arrival_date: date, items, *, status="paid", reference="BUILD BRIGHTON") -> dict:
        """Tests and development: `items` are (amount pence, fee pence, PaymentIntent id or "")."""
        listed = [
            {
                "kind": "payment" if amount > 0 else "refund",
                "amount_pence": amount,
                "fee_pence": fee,
                "payment_id": payment_id,
                "description": "",
            }
            for amount, fee, payment_id in items
        ]
        payout = {
            "id": f"po_fake{self.next_id:06d}",
            "amount_pence": sum(i["amount_pence"] - i["fee_pence"] for i in listed),
            "fees_pence": sum(i["fee_pence"] for i in listed),
            "arrival_date": arrival_date.isoformat(),
            "status": status,
            "reference": reference,
            "items": listed,
        }
        self.next_id += 1
        self.payouts_made.append(payout)
        self._save()
        return {**payout, "arrival_date": arrival_date}

    def pay_out(self, arrival_date: date) -> dict | None:
        """Development: one payout of every paid checkout not yet paid out."""
        done = {item["payment_id"] for payout in self.payouts_made for item in payout["items"]}
        items = []
        for session_id, session in self.sessions.items():
            payment_id = f"pi_fake_{session_id[-6:]}"
            if session["status"] == "paid" and payment_id not in done:
                items.append((session["amount"], self._fee(session["amount"]), payment_id))
        return self.add_payout(arrival_date, items) if items else None

    def payouts(self, since: date) -> list[dict]:
        if self.fail_next:
            message, self.fail_next = self.fail_next, ""
            raise CardError(message)
        found = []
        for payout in self.payouts_made:
            arrival = date.fromisoformat(payout["arrival_date"])
            if arrival >= since:
                found.append({**payout, "arrival_date": arrival})
        return found


class NotConfigured:
    def __getattr__(self, name):
        def refuse(*args, **kwargs):
            logger.error("Stripe isn't configured (STRIPE_SECRET_KEY): can't %s", name)
            raise CardError("Card payments aren't available on this site at the moment.")

        return refuse


_fake = FakeCards()


def client():
    if settings.STRIPE_SECRET_KEY:
        return StripeCards(settings.STRIPE_SECRET_KEY)
    if fakes.allowed() or getattr(settings, "TESTING", False):
        return fake()
    return NotConfigured()


def fake() -> FakeCards:
    global _fake
    if fakes.allowed() and not getattr(settings, "TESTING", False):
        _fake = FakeCards(path=fakes.state_file(".fake-stripe.json")).load()
    return _fake


def reset_fake() -> FakeCards:
    global _fake
    _fake = FakeCards()
    return _fake
