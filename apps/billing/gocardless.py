"""
Talking to GoCardless (spec 04 §4.2).

Everything BBMS asks of GoCardless goes through `client()`, which returns one of:
- `GoCardlessClient`: the real API (the official `gocardless-pro` library), when `GOCARDLESS_ACCESS_TOKEN` is set.
  Use a **sandbox** token (`GOCARDLESS_ENVIRONMENT=sandbox`, the default) everywhere except the live site.
- `FakeGoCardless`: when developing or testing without a token. It keeps what it's asked in memory, and its
  "hosted page" is a local page (`/dev/gocardless/…`, DEBUG only) with an "Authorise" button.
- `DryRunGoCardless`: inside a job's `--dry-run` (and on the shadow site), which sends nothing.

Direct Debit set-up uses a **billing request flow** (GoCardless's current hosted set-up pages). The spec's "redirect
flow" is GoCardless's older API for the same thing; mandates made either way work the same.
"""

import contextvars
import itertools
import json
import logging
import os
from dataclasses import dataclass, field

from django.conf import settings

logger = logging.getLogger(__name__)


class GoCardlessError(Exception):
    """GoCardless refused or couldn't be reached. The message is safe to show an admin."""


@dataclass
class CreatedPayment:
    id: str
    status: str


class GoCardlessClient:
    """The real API."""

    def __init__(self, access_token, environment):
        import gocardless_pro

        self._api = gocardless_pro.Client(access_token=access_token, environment=environment)

    def _call(self, what, fn, *args, **kwargs):
        import gocardless_pro.errors

        try:
            return fn(*args, **kwargs)
        except gocardless_pro.errors.GoCardlessProError as error:
            logger.warning("GoCardless refused to %s: %s", what, error)
            raise GoCardlessError(f"GoCardless couldn't {what}: {error}") from error
        except OSError as error:  # network
            raise GoCardlessError(f"Couldn't reach GoCardless to {what}.") from error

    def start_mandate_setup(self, member, *, redirect_uri, exit_uri) -> tuple[str, str]:
        """Returns (billing request id, URL of GoCardless's page to send the member to)."""
        request = self._call(
            "start a Direct Debit set-up",
            self._api.billing_requests.create,
            params={"mandate_request": {"scheme": "bacs"}, "metadata": {"member": str(member.pk)}},
        )
        address = member.current_address or member.addresses.first()
        prefilled = {"given_name": member.given_name, "family_name": member.family_name, "email": member.email}
        if address is not None:
            prefilled.update(address_line1=address.line_1, postal_code=address.postcode)
        flow = self._call(
            "start a Direct Debit set-up",
            self._api.billing_request_flows.create,
            params={
                "redirect_uri": redirect_uri,
                "exit_uri": exit_uri,
                "links": {"billing_request": request.id},
                "prefilled_customer": prefilled,
            },
        )
        return request.id, flow.authorisation_url

    def mandate_for(self, billing_request_id) -> str | None:
        """The mandate a finished set-up made, or None if the member hasn't finished."""
        request = self._call("check a Direct Debit set-up", self._api.billing_requests.get, billing_request_id)
        if request.status != "fulfilled":
            return None
        return request.links.mandate_request_mandate

    def create_payment(self, mandate_id, amount_pence, description, *, idempotency_key, metadata) -> CreatedPayment:
        payment = self._call(
            "take a Direct Debit payment",
            self._api.payments.create,
            params={
                "amount": amount_pence,
                "currency": "GBP",
                "description": description,
                "links": {"mandate": mandate_id},
                "metadata": metadata,
            },
            headers={"Idempotency-Key": idempotency_key},
        )
        return CreatedPayment(payment.id, payment.status)

    def retry_payment(self, payment_id):
        self._call("retry a Direct Debit payment", self._api.payments.retry, payment_id)

    def cancel_mandate(self, mandate_id):
        self._call("cancel a Direct Debit", self._api.mandates.cancel, mandate_id)


@dataclass
class FakeGoCardless:
    """Behaves like GoCardless for local development and tests. Nothing leaves the machine.

    When developing, its state is kept in a file (`path`), so the web server and `manage.py` jobs share it.
    """

    path: str | None = None
    billing_requests: dict = field(default_factory=dict)  # id -> {"member", "redirect_uri", "exit_uri", "mandate"}
    payments: dict = field(default_factory=dict)  # id -> {"mandate", "amount", "description", "status", "metadata"}
    cancelled_mandates: list = field(default_factory=list)
    retried: list = field(default_factory=list)
    idempotency: dict = field(default_factory=dict)
    next_id: int = 1
    fail_next: str = ""  # tests: make the next call raise GoCardlessError with this message

    STATE = ("billing_requests", "payments", "cancelled_mandates", "retried", "idempotency", "next_id")

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

    def _new_id(self, prefix):
        value = f"{prefix}{self.next_id:06d}"
        self.next_id += 1
        return value

    def _maybe_fail(self):
        if self.fail_next:
            message, self.fail_next = self.fail_next, ""
            raise GoCardlessError(message)

    def start_mandate_setup(self, member, *, redirect_uri, exit_uri):
        self._maybe_fail()
        request_id = self._new_id("BRQ")
        self.billing_requests[request_id] = {
            "member": member.pk,
            "redirect_uri": redirect_uri,
            "exit_uri": exit_uri,
            "mandate": None,
        }
        self._save()
        return request_id, f"/dev/gocardless/{request_id}/"

    def authorise(self, request_id) -> str:
        """What the member does on GoCardless's page. Returns where GoCardless sends them back to."""
        request = self.billing_requests[request_id]
        request["mandate"] = request["mandate"] or self._new_id("MD")
        self._save()
        return request["redirect_uri"]

    def mandate_for(self, billing_request_id):
        self._maybe_fail()
        request = self.billing_requests.get(billing_request_id)
        if request is None:
            raise GoCardlessError("GoCardless couldn't check a Direct Debit set-up: not found.")
        return request["mandate"]

    def create_payment(self, mandate_id, amount_pence, description, *, idempotency_key, metadata):
        self._maybe_fail()
        if idempotency_key in self.idempotency:
            return CreatedPayment(self.idempotency[idempotency_key], "pending_submission")
        if mandate_id in self.cancelled_mandates:
            raise GoCardlessError("GoCardless couldn't take a Direct Debit payment: the mandate is cancelled.")
        payment_id = self._new_id("PM")
        self.payments[payment_id] = {
            "mandate": mandate_id,
            "amount": amount_pence,
            "description": description,
            "status": "pending_submission",
            "metadata": metadata,
        }
        self.idempotency[idempotency_key] = payment_id
        self._save()
        return CreatedPayment(payment_id, "pending_submission")

    def retry_payment(self, payment_id):
        self._maybe_fail()
        self.retried.append(payment_id)
        self.payments[payment_id]["status"] = "pending_submission"
        self._save()

    def cancel_mandate(self, mandate_id):
        self._maybe_fail()
        self.cancelled_mandates.append(mandate_id)
        self._save()

    def set_status(self, payment_id, status):
        self.payments[payment_id]["status"] = status
        self._save()


class DryRunGoCardless:
    """Sends nothing: a dry run works out what would happen."""

    def __init__(self):
        self._ids = itertools.count(1)
        self.calls = []

    def create_payment(self, mandate_id, amount_pence, description, *, idempotency_key, metadata):
        self.calls.append(("create_payment", mandate_id, amount_pence))
        return CreatedPayment(f"DRYRUN{next(self._ids)}", "pending_submission")

    def retry_payment(self, payment_id):
        self.calls.append(("retry_payment", payment_id))

    def cancel_mandate(self, mandate_id):
        self.calls.append(("cancel_mandate", mandate_id))

    def start_mandate_setup(self, *args, **kwargs):
        raise GoCardlessError("Not during a dry run.")

    def mandate_for(self, *args, **kwargs):
        raise GoCardlessError("Not during a dry run.")


class NotConfigured:
    """A site without GOCARDLESS_ACCESS_TOKEN (and not developing): every call fails politely, so members see
    "Direct Debit isn't available" and jobs log it, rather than a server error. `check --deploy` warns about it.
    """

    def __getattr__(self, name):
        def refuse(*args, **kwargs):
            logger.error("GoCardless isn't configured (GOCARDLESS_ACCESS_TOKEN): can't %s", name)
            raise GoCardlessError("Direct Debit isn't available on this site at the moment.")

        return refuse


_fake = FakeGoCardless()
_override = contextvars.ContextVar("gocardless_override", default=None)


def client():
    override = _override.get()
    if override is not None:
        return override
    if settings.GOCARDLESS_ACCESS_TOKEN:
        return GoCardlessClient(settings.GOCARDLESS_ACCESS_TOKEN, settings.GOCARDLESS_ENVIRONMENT)
    if settings.DEBUG or getattr(settings, "TESTING", False):
        return fake()
    return NotConfigured()


def fake() -> FakeGoCardless:
    """The fake used when there's no token. When developing (not testing) it's read from a file each time."""
    global _fake
    if settings.DEBUG and not getattr(settings, "TESTING", False):
        _fake = FakeGoCardless(path=str(settings.BASE_DIR / ".fake-gocardless.json")).load()
    return _fake


def reset_fake():
    global _fake
    _fake = FakeGoCardless()
    return _fake


class use:
    """`with gocardless.use(DryRunGoCardless()):` – send this context's GoCardless calls somewhere else."""

    def __init__(self, replacement):
        self.replacement = replacement

    def __enter__(self):
        self._token = _override.set(self.replacement)
        return self.replacement

    def __exit__(self, *exc):
        _override.reset(self._token)
