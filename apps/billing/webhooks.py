"""
GoCardless webhooks (spec 04 §4.2) and finishing a Direct Debit set-up.

The webhook stores each event (ignoring ones it has seen before), then handles the stored events in the order
GoCardless created them. An event that fails is kept with its error for someone to look at (admin: Subscriptions and
billing → GoCardless events), and `manage.py process_gocardless_events --retry-failed` tries it again.
"""

import hashlib
import hmac
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.members.models import Member
from apps.payments.models import Payment
from apps.site_settings.models import SiteSettings

from . import emails, gocardless, membership
from .billing import locked_rows
from .models import Charge, GoCardlessEvent, MandateSetup

logger = logging.getLogger(__name__)

# Failures worth asking GoCardless to try again (03 §3.6). Never a cancelled mandate or a bank's refusal.
RETRYABLE_CAUSES = {"insufficient_funds", "refer_to_payer", "test_failure"}
CAUSES = {
    "insufficient_funds": "not enough money in the account",
    "refer_to_payer": "the bank refused it",
    "mandate_cancelled": "the Direct Debit was cancelled",
    "bank_account_closed": "the bank account is closed",
}


# --- finishing a set-up (from the return page or the billing_requests.fulfilled event) ---


def finish_setup(setup: MandateSetup, mandate_id: str | None = None, today=None) -> bool:
    """Record the member's new mandate and activate them. Returns False if they haven't finished at GoCardless."""
    if setup.completed_at:
        return True
    mandate_id = mandate_id or gocardless.client().mandate_for(setup.billing_request_id)
    if not mandate_id:
        return False
    with transaction.atomic():
        setup = MandateSetup.objects.select_for_update().get(pk=setup.pk)
        if setup.completed_at:
            return True
        member = Member.objects.select_for_update().get(pk=setup.member_id)
        old = member.mandate_id
        member.mandate_id = mandate_id
        if member.payment_method != Member.PaymentMethod.CREDIT:
            member.payment_method = Member.PaymentMethod.DIRECT_DEBIT  # a credit payer keeps it as a backup
        if member.payment_day is None:
            member.payment_day = membership.payment_day_for(today or timezone.localdate())
        member.save(update_fields=["mandate_id", "payment_method", "payment_day"])
        setup.mandate_id = mandate_id
        setup.completed_at = timezone.now()
        setup.save(update_fields=["mandate_id", "completed_at"])
        membership.activate(member, today)
    if old and old != mandate_id:
        try:
            gocardless.client().cancel_mandate(old)
        except gocardless.GoCardlessError as error:
            logger.warning("Member %s: couldn't cancel the replaced mandate: %s", member.pk, error)
    return True


# --- the webhook ---


def signature_ok(body: bytes, signature: str) -> bool:
    secret = settings.GOCARDLESS_WEBHOOK_SECRET
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)  # constant time


@csrf_exempt
@require_POST
def webhook(request):
    if not signature_ok(request.body, request.headers.get("Webhook-Signature", "")):
        logger.warning("GoCardless webhook with a bad signature refused")
        return HttpResponse(status=403)
    try:
        events = json.loads(request.body)["events"]
    except (ValueError, KeyError, TypeError):
        return HttpResponse(status=400)
    stored = store_events(events)
    process_pending()
    logger.info("GoCardless webhook: %s events, %s new", len(events), stored)
    return HttpResponse(status=204)


def store_events(events) -> int:
    new = 0
    for event in events:
        resource_type = event.get("resource_type", "")
        links = event.get("links") or {}
        # The link named after the resource ("payments" → "payment"), or a billing request's.
        resource_id = links.get(resource_type.rstrip("s"), "") or links.get("billing_request", "")
        _, created = GoCardlessEvent.objects.get_or_create(
            event_id=event["id"],
            defaults={
                "resource_type": resource_type,
                "action": event.get("action", ""),
                "resource_id": resource_id,
                "created_at": parse_datetime(event.get("created_at", "")) or timezone.now(),
                "payload": event,
            },
        )
        new += created
    return new


def process_pending(retry_failed=False) -> int:
    """Handle stored events in the order GoCardless created them. Returns how many were handled."""
    pending = GoCardlessEvent.objects.filter(processed_at__isnull=True)
    if not retry_failed:
        pending = pending.filter(error="")
    count = 0
    for event_id in pending.order_by("created_at", "pk").values_list("pk", flat=True):
        try:
            with transaction.atomic():
                event = GoCardlessEvent.objects.select_for_update().get(pk=event_id)
                if event.processed_at:
                    continue
                event.outcome = handle(event) or "logged"
                event.processed_at = timezone.now()
                event.error = ""
                event.save(update_fields=["outcome", "processed_at", "error"])
            count += 1
        except Exception as error:  # keep going; a person looks at the failed one
            logger.exception("GoCardless event %s failed", event_id)
            GoCardlessEvent.objects.filter(pk=event_id).update(error=f"{type(error).__name__}: {error}")
    return count


def handle(event: GoCardlessEvent) -> str:
    kind = (event.resource_type, event.action)
    details = event.payload.get("details") or {}
    links = event.payload.get("links") or {}
    when = event.created_at
    if event.resource_type == "payments":
        return _payment_event(event.resource_id, event.action, details, when)
    if kind in (("mandates", "cancelled"), ("mandates", "failed"), ("mandates", "expired")):
        member = Member.objects.filter(mandate_id=event.resource_id).first()
        if member is None:
            return "no member has this mandate"
        membership.mandate_gone(member)
        return f"member {member.pk}: mandate removed"
    if kind == ("mandates", "replaced"):
        new = links.get("new_mandate", "")
        updated = Member.objects.filter(mandate_id=event.resource_id).update(mandate_id=new) if new else 0
        return f"{updated} member(s) moved to the new mandate"
    if kind == ("billing_requests", "fulfilled"):
        setup = MandateSetup.objects.filter(billing_request_id=event.resource_id).first()
        if setup is None:
            return "not a set-up BBMS started"
        finish_setup(setup, links.get("mandate_request_mandate"), timezone.localdate(when))
        return f"member {setup.member_id}: Direct Debit set up"
    return ""


def _payment_event(payment_id, action, details, when) -> str:
    rows = locked_rows(payment_id)
    if not rows.exists():
        return "not a payment BBMS knows"
    day = timezone.localdate(when)
    charges = list(Charge.objects.filter(payments__in=rows).distinct().select_related("member", "rental__locker"))
    if action in ("submitted", "resubmission_requested"):
        rows.update(status=Payment.Status.PENDING)
        return "pending"
    if action == "confirmed":
        rows.update(status=Payment.Status.PAID, paid_at=when)
        for charge in charges:
            if charge.status != Charge.Status.PAID:
                membership.charge_paid(charge, day)
        return f"paid ({len(charges)} charge(s))"
    if action in ("failed", "cancelled", "charged_back"):
        rows.update(status=Payment.Status.CANCELLED if action == "cancelled" else Payment.Status.FAILED)
        return _payment_failed(rows, charges, action, details, day)
    return ""


def _payment_failed(rows, charges, action, details, day) -> str:
    config = SiteSettings.load()
    cause = details.get("cause", "")
    retry_on = None
    if action == "failed":
        if config.dd_retry_mode == SiteSettings.RetryMode.GOCARDLESS:
            will_retry = bool(details.get("will_attempt_retry"))
        else:
            # BBMS only retries subscription and rent charges; a one-off payment (a fee, a top-up) isn't retried.
            will_retry = (
                bool(charges) and cause in RETRYABLE_CAUSES and all(c.retries < config.dd_retries for c in charges)
            )
            if will_retry:
                retry_on = day + timedelta(days=config.dd_retry_after_days)
    else:
        will_retry = False
    for charge in charges:
        charge.failure_cause = cause[:100]
        charge.save(update_fields=["failure_cause"])
        if will_retry:
            charge.status = Charge.Status.PROCESSING
            charge.retry_on = retry_on
            charge.save(update_fields=["status", "retry_on"])
        elif charge.status != Charge.Status.PAID or action == "charged_back":
            membership.charge_failed(charge, day)
    if not will_retry:
        # A one-off induction fee that wasn't paid after all: the member isn't paid up for that induction.
        from apps.equipment.models import Induction

        Induction.objects.filter(payment__in=rows).update(paid=False)
        from apps.storage import rules as storage

        storage.box_payment_failed(rows)
    first = rows.select_related("member").first()
    if first is not None and first.member is not None:
        member = first.member
        total = sum(abs(r.amount_pence) for r in rows)
        kinds = {r.get_reason_display().lower() for r in rows}
        what = " and ".join(sorted(kinds, reverse=True))
        emails.send(
            member,
            "direct_debit_failed",
            amount=f"{total / 100:.2f}",
            what=what,
            cause=CAUSES.get(cause, ""),
            retry_on=retry_on,
        )
    return f"{action} ({cause or 'no cause'}); {'will retry' if will_retry else 'final'}"
