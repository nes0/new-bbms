"""GoCardless webhooks (spec 04 §4.2) and failed Direct Debit retries (03 §3.6)."""

import hashlib
import hmac
import json
from datetime import date, timedelta

from django.contrib.auth.models import Group
from django.core import mail
from django.test import override_settings

from apps.billing import billing, membership
from apps.billing.models import Charge, GoCardlessEvent, MandateSetup
from apps.members.models import Member
from apps.payments.models import Payment
from apps.site_settings.models import SiteSettings

from .helpers import TODAY, WEBHOOK_SECRET, BillingTestCase, payer

_count = 0


def event(resource_type, action, links, details=None, created_at="2026-12-12T10:00:00.000Z"):
    global _count
    _count += 1
    return {
        "id": f"EV{_count:06d}",
        "created_at": created_at,
        "resource_type": resource_type,
        "action": action,
        "links": links,
        "details": details or {},
    }


class WebhookTestCase(BillingTestCase):
    def send(self, *events, secret=WEBHOOK_SECRET):
        body = json.dumps({"events": list(events)}).encode()
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post(
            "/gocardless/webhook", body, content_type="application/json", headers={"Webhook-Signature": signature}
        )

    def billed(self, method=Member.PaymentMethod.DIRECT_DEBIT, **fields):
        """A Direct Debit payer whose subscription has just been billed. Returns (member, charge, payment id)."""
        who = payer(method, **fields)
        billing.create_charges(TODAY)
        billing.bill_members(TODAY)
        mail.outbox.clear()
        charge = who.charges.get(kind=Charge.Kind.SUBSCRIPTION)
        return who, charge, charge.payments.get().source_id


class SignatureTests(WebhookTestCase):
    def test_signature_must_match(self):
        self.assertEqual(self.send(event("payments", "created", {"payment": "PM1"}), secret="wrong").status_code, 403)
        self.assertEqual(self.send(event("payments", "created", {"payment": "PM1"})).status_code, 204)

    @override_settings(GOCARDLESS_WEBHOOK_SECRET="")
    def test_refused_without_a_secret(self):
        self.assertEqual(self.send(event("payments", "created", {"payment": "PM1"}), secret="").status_code, 403)

    def test_bad_json_is_400(self):
        body = b"not json"
        signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        response = self.client.post(
            "/gocardless/webhook", body, content_type="application/json", headers={"Webhook-Signature": signature}
        )
        self.assertEqual(response.status_code, 400)


class PaymentEventTests(WebhookTestCase):
    def test_confirmed_pays_the_charge(self):
        who, charge, payment_id = self.billed()
        self.send(event("payments", "confirmed", {"payment": payment_id}))
        charge.refresh_from_db()
        who.refresh_from_db()
        self.assertEqual((charge.status, charge.paid_on), ("paid", date(2026, 12, 12)))
        self.assertEqual(charge.payments.get().status, "paid")
        self.assertEqual(who.subscription_expires, date(2027, 1, 12))

    def test_confirmed_reactivates_a_suspended_member(self):
        who, charge, payment_id = self.billed()
        who.status = Member.Status.SUSPENDED
        who.save()
        self.send(event("payments", "confirmed", {"payment": payment_id}))
        who.refresh_from_db()
        self.assertEqual(who.status, "active")

    def test_duplicate_events_are_ignored(self):
        _, charge, payment_id = self.billed()
        failed = event("payments", "failed", {"payment": payment_id}, {"cause": "insufficient_funds"})
        self.send(failed)
        self.send(failed)
        self.assertEqual(GoCardlessEvent.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_events_are_handled_in_the_order_gocardless_made_them(self):
        _, charge, payment_id = self.billed()
        # Sent out of order: the later "confirmed" must win over the earlier "submitted".
        self.send(
            event("payments", "confirmed", {"payment": payment_id}, created_at="2026-12-13T10:00:00Z"),
            event("payments", "submitted", {"payment": payment_id}, created_at="2026-12-11T10:00:00Z"),
        )
        self.assertEqual(charge.payments.get().status, "paid")

    def test_unknown_payment_is_just_logged(self):
        self.send(event("payments", "confirmed", {"payment": "PM-NOT-OURS"}))
        self.assertEqual(GoCardlessEvent.objects.get().outcome, "not a payment BBMS knows")


class FailedDirectDebitTests(WebhookTestCase):
    def test_bbms_retries_once_after_5_days_then_gives_up(self):
        who, charge, payment_id = self.billed()
        self.send(event("payments", "failed", {"payment": payment_id}, {"cause": "insufficient_funds"}))
        charge.refresh_from_db()
        self.assertEqual((charge.status, charge.retry_on), ("processing", date(2026, 12, 17)))
        self.assertIn("We'll try again on 17 December", mail.outbox[0].body)

        self.assertEqual(billing.retry_direct_debits(date(2026, 12, 16)), 0)
        self.assertEqual(billing.retry_direct_debits(date(2026, 12, 17)), 1)
        self.assertEqual(self.gc.retried, [payment_id])
        charge.refresh_from_db()
        self.assertEqual((charge.retries, charge.retry_on), (1, None))
        self.assertEqual(charge.payments.get().status, "pending")

        self.send(event("payments", "failed", {"payment": payment_id}, {"cause": "insufficient_funds"}))
        charge.refresh_from_db()
        who.refresh_from_db()
        self.assertEqual(charge.status, "failed")
        self.assertEqual(who.subscription_expires, date(2026, 12, 12))  # back to the failure date
        self.assertIn("We won't try again", mail.outbox[1].body)

    def test_failures_not_worth_retrying_are_final(self):
        _, charge, payment_id = self.billed()
        self.send(event("payments", "failed", {"payment": payment_id}, {"cause": "bank_account_closed"}))
        charge.refresh_from_db()
        self.assertEqual(charge.status, "failed")

    def test_gocardless_retries_when_set_to(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"dd_retry_mode": "gocardless"})
        _, charge, payment_id = self.billed()
        self.send(
            event(
                "payments",
                "failed",
                {"payment": payment_id},
                {"cause": "insufficient_funds", "will_attempt_retry": True},
            )
        )
        charge.refresh_from_db()
        self.assertEqual((charge.status, charge.retry_on), ("processing", None))
        self.send(event("payments", "resubmission_requested", {"payment": payment_id}))
        self.send(event("payments", "confirmed", {"payment": payment_id}))
        charge.refresh_from_db()
        self.assertEqual(charge.status, "paid")

    def test_a_failed_charge_can_be_paid_from_credit(self):
        who, charge, payment_id = self.billed()
        self.send(event("payments", "failed", {"payment": payment_id}, {"cause": "bank_account_closed"}))
        Payment.objects.create(member=who, reason="balance", source="cash", amount_pence=5000)
        billing.pay_charge_from_credit(charge, date(2026, 12, 20))
        charge.refresh_from_db()
        who.refresh_from_db()
        self.assertEqual((charge.status, who.subscription_expires), ("paid", date(2027, 1, 20)))

    def test_unpaid_rent_tells_the_storage_team(self):
        team = Group.objects.create(name="storage")
        payer(subscription_expires=TODAY).groups.add(team)
        who = payer(mandate_id="MD-RENT")
        self.rent_locker(who)
        billing.create_charges(TODAY)
        billing.bill_members(TODAY)
        mail.outbox.clear()
        payment_id = Payment.objects.filter(member=who).first().source_id
        self.send(event("payments", "cancelled", {"payment": payment_id}))
        self.assertEqual(set(who.charges.values_list("status", flat=True)), {"failed"})
        self.assertIn("locker rent not paid", " ".join(m.subject for m in mail.outbox))


class MandateEventTests(WebhookTestCase):
    def test_cancelled_mandate_means_leaving(self):
        who, charge, _ = self.billed()
        Charge.objects.create(member=who, charge_date=TODAY + timedelta(days=30))
        self.send(event("mandates", "cancelled", {"mandate": who.mandate_id}))
        who.refresh_from_db()
        self.assertEqual((who.status, who.mandate_id, who.payment_method), ("leaving", "", ""))
        self.assertEqual(who.charges.get(charge_date=TODAY + timedelta(days=30)).status, "cancelled")
        self.assertEqual(self.gc.cancelled_mandates, [])  # GoCardless did it already

    def test_a_credit_payer_just_loses_the_backup(self):
        who = payer(Member.PaymentMethod.CREDIT, mandate_id="MD-BACKUP")
        self.send(event("mandates", "cancelled", {"mandate": "MD-BACKUP"}))
        who.refresh_from_db()
        self.assertEqual((who.status, who.mandate_id, who.payment_method), ("active", "", "balance"))

    def test_replaced_mandate_is_followed(self):
        who = payer(mandate_id="MD-OLD")
        self.send(event("mandates", "replaced", {"mandate": "MD-OLD", "new_mandate": "MD-NEW"}))
        who.refresh_from_db()
        self.assertEqual(who.mandate_id, "MD-NEW")

    def test_a_finished_set_up_is_picked_up_from_the_webhook(self):
        who = payer(Member.PaymentMethod.NONE, status=Member.Status.SETTING_UP, mandate_id="", day=None)
        setup = MandateSetup.objects.create(member=who, billing_request_id="BRQ-1")
        self.send(
            event("billing_requests", "fulfilled", {"billing_request": "BRQ-1", "mandate_request_mandate": "MD-9"})
        )
        who.refresh_from_db()
        setup.refresh_from_db()
        self.assertEqual((who.status, who.mandate_id, who.payment_method), ("active", "MD-9", "gocardless-variable"))
        self.assertEqual(who.payment_day, 12)  # the event's day
        self.assertIsNotNone(setup.completed_at)


class FailedEventTests(WebhookTestCase):
    def test_an_event_that_fails_is_kept_and_the_rest_carry_on(self):
        who, charge, payment_id = self.billed()
        original = membership.mandate_gone

        def broken(member):
            raise RuntimeError("boom")

        membership.mandate_gone = broken
        try:
            self.send(
                event("mandates", "cancelled", {"mandate": who.mandate_id}, created_at="2026-12-12T09:00:00Z"),
                event("payments", "confirmed", {"payment": payment_id}),
            )
        finally:
            membership.mandate_gone = original
        failed = GoCardlessEvent.objects.get(resource_type="mandates")
        self.assertIn("boom", failed.error)
        self.assertIsNone(failed.processed_at)
        charge.refresh_from_db()
        self.assertEqual(charge.status, "paid")
        from apps.billing.webhooks import process_pending

        self.assertEqual(process_pending(retry_failed=True), 1)
        who.refresh_from_db()
        self.assertEqual(who.status, "leaving")
