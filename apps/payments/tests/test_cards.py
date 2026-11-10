"""Paying by card through Stripe Checkout, and paying fees from credit (spec 04 §4.1, §4.3)."""

import hashlib
import hmac
import json
import time

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.billing.membership import add_month
from apps.billing.models import Charge
from apps.equipment.models import Equipment, Induction
from apps.equipment.tests.helpers import member
from apps.members.models import IdCheck, Member
from apps.payments import cards, checkout
from apps.payments.credit import credit_pence
from apps.payments.models import CardCheckout, Payment, StripeEvent
from apps.payments.services import record_payment

SECRET = "whsec_test_secret"


@override_settings(STRIPE_SECRET_KEY="", STRIPE_WEBHOOK_SECRET=SECRET)
class CardTestCase(TestCase):
    def setUp(self):
        self.stripe = cards.reset_fake()
        self.member = member(
            trusted=True, induction_completed=True, inducted_at=timezone.now(), monthly_subscription_pence=2500
        )
        IdCheck.objects.create(member=self.member, result=IdCheck.Result.APPROVED, documents_seen="photo ID")
        self.client.force_login(self.member)

    def start(self, item, **data):
        return self.client.post("/pay/card/", {"item": item, **data})

    def session_id(self):
        return CardCheckout.objects.latest("pk").session_id

    def pay_and_return(self):
        """The member pays on Stripe's page and is sent back to BBMS."""
        back = self.stripe.pay(self.session_id())
        return self.client.get(back)

    def webhook(self, event_type, session_id, event_id="evt_1", secret=SECRET):
        payload = json.dumps(
            {
                "id": event_id,
                "object": "event",
                "type": event_type,
                "data": {"object": {"id": session_id, "object": "checkout.session"}},
            }
        )
        stamp = int(time.time())
        signature = hmac.new(secret.encode(), f"{stamp}.{payload}".encode(), hashlib.sha256).hexdigest()
        return self.client.post(
            "/stripe/webhook",
            payload,
            content_type="application/json",
            headers={"Stripe-Signature": f"t={stamp},v1={signature}"},
        )


class PayablesTests(CardTestCase):
    def test_what_a_member_can_pay(self):
        laser = Equipment.objects.create(name="Laser", slug="laser", requires_induction=True, induction_fee_pence=1000)
        Equipment.objects.create(name="Lathe", slug="lathe", requires_induction=True, induction_fee_pence=0)
        charge = Charge.objects.create(member=self.member, charge_date=timezone.localdate(), status="failed")
        items = {item.key: item.amount_pence for item in checkout.payables(self.member)}
        self.assertEqual(
            items,
            {f"charge:{charge.pk}": 2500, "key-deposit": 1500, "storage-box": 1500, f"induction:{laser.pk}": 1000},
        )

    def test_no_key_deposit_until_the_general_induction_is_confirmed(self):
        self.member.inducted_at = None
        self.member.save()
        self.assertNotIn("key-deposit", [item.key for item in checkout.payables(self.member)])

    def test_no_key_deposit_until_the_proof_of_identity_is_checked(self):
        self.member.id_checks.all().delete()
        self.assertNotIn("key-deposit", [item.key for item in checkout.payables(self.member)])
        IdCheck.objects.create(member=self.member, result=IdCheck.Result.REJECTED, documents_seen="photo ID")
        self.assertNotIn("key-deposit", [item.key for item in checkout.payables(self.member)])

    def test_paid_things_and_untrusted_members(self):
        self.member.trusted = False
        self.member.save()
        record_payment(self.member, Payment.Reason.STORAGE_BOX, Payment.Source.CASH, 1500)
        self.member.refresh_from_db()  # the box flag was set on another copy of the member
        self.assertEqual(checkout.payables(self.member), [])

    def test_only_charges_for_someone_who_isnt_a_current_member(self):
        self.member.status = Member.Status.SUSPENDED
        self.member.save()
        Charge.objects.create(member=self.member, charge_date=timezone.localdate(), status="due")
        self.assertEqual([i.purpose for i in checkout.payables(self.member)], ["charge"])


class TopUpTests(CardTestCase):
    def test_top_up_by_card(self):
        response = self.start("top-up", amount="20")
        self.assertRedirects(response, f"/dev/stripe/{self.session_id()}/", fetch_redirect_response=False)
        self.assertEqual(self.stripe.sessions[self.session_id()]["amount"], 2000)
        self.assertRedirects(self.pay_and_return(), "/credit/")
        payment = Payment.objects.get()
        self.assertEqual((payment.reason, payment.source, payment.status), ("balance", "stripe", "paid"))
        self.assertEqual(payment.fee_pence, 50)  # the fake's 1.5% + 20p: the real fee from Stripe in production
        self.assertTrue(payment.source_id.startswith("pi_"))
        self.assertEqual(credit_pence(self.member), 2000)

    def test_limits(self):
        for amount in ("4.99", "200.01", "-5"):
            self.start("top-up", amount=amount)
        self.assertFalse(CardCheckout.objects.exists())
        self.start("top-up", amount="5")
        self.assertEqual(CardCheckout.objects.count(), 1)

    def test_coming_back_twice_or_with_the_webhook_records_it_once(self):
        self.start("top-up", amount="10")
        self.pay_and_return()
        self.client.get(f"/pay/card/done/?session_id={self.session_id()}")
        self.webhook("checkout.session.completed", self.session_id())
        self.assertEqual(Payment.objects.count(), 1)

    def test_cancelled_or_not_paid_yet(self):
        self.start("top-up", amount="10")
        response = self.client.get(f"/pay/card/done/?session_id={self.session_id()}", follow=True)
        self.assertContains(response, "hasn&#x27;t confirmed your payment yet")
        self.assertFalse(Payment.objects.exists())

    def test_someone_elses_checkout_is_ignored(self):
        self.start("top-up", amount="10")
        self.stripe.pay(self.session_id())
        self.client.force_login(member())
        self.client.get(f"/pay/card/done/?session_id={self.session_id()}")
        self.assertFalse(Payment.objects.exists())

    def test_stripe_down(self):
        self.stripe.fail_next = "Stripe couldn't start a card payment. Please try again later."
        response = self.start("top-up", amount="10")
        self.assertRedirects(response, "/pay/", fetch_redirect_response=False)
        self.assertEqual(CardCheckout.objects.get().status, "expired")


class FixedPriceTests(CardTestCase):
    def test_the_price_comes_from_bbms_not_the_browser(self):
        self.start("key-deposit", amount="0.01")
        self.assertEqual(CardCheckout.objects.get().amount_pence, 1500)
        self.pay_and_return()
        payment = Payment.objects.get()
        self.assertEqual((payment.reason, payment.amount_pence, payment.source), ("key-deposit", 1500, "stripe"))
        self.assertEqual(credit_pence(self.member), 0)

    def test_induction_fee(self):
        laser = Equipment.objects.create(name="Laser", slug="laser", requires_induction=True, induction_fee_pence=1000)
        self.start(f"induction:{laser.pk}")
        self.pay_and_return()
        self.assertTrue(Induction.objects.get(member=self.member, category=laser.induction_key).paid)

    def test_a_failed_subscription(self):
        charge = Charge.objects.create(member=self.member, charge_date=timezone.localdate(), status="failed")
        self.start(f"charge:{charge.pk}")
        self.assertRedirects(self.pay_and_return(), "/account/payment/")
        charge.refresh_from_db()
        self.member.refresh_from_db()
        self.assertEqual((charge.status, charge.amount_pence), ("paid", 2500))
        self.assertEqual(self.member.subscription_expires, add_month(timezone.localdate()))
        self.assertEqual(charge.payments.get().source, "stripe")

    def test_already_paid_another_way_goes_to_credit(self):
        self.start("storage-box")
        record_payment(self.member, Payment.Reason.STORAGE_BOX, Payment.Source.CASH, 1500)
        response = self.pay_and_return()
        self.assertEqual(credit_pence(self.member), 1500)
        checkout_row = CardCheckout.objects.get()
        self.assertIn("already paid, so this went to credit", checkout_row.note)
        self.assertEqual(checkout_row.payment.reason, "balance")
        self.assertEqual(response.status_code, 302)

    def test_nothing_to_pay(self):
        response = self.client.post("/pay/card/", {"item": "induction:999"}, follow=True)
        self.assertContains(response, "doesn&#x27;t need paying any more")


class CreditTests(CardTestCase):
    def test_pay_from_credit(self):
        response = self.client.post("/pay/credit/", {"item": "key-deposit"}, follow=True)
        self.assertContains(response, "Top up your credit first")
        record_payment(self.member, Payment.Reason.BALANCE, Payment.Source.CASH, 2000)
        self.client.post("/pay/credit/", {"item": "key-deposit"})
        self.assertEqual(credit_pence(self.member), 500)
        self.assertNotIn("key-deposit", [i.key for i in checkout.payables(self.member)])

    def test_pages(self):
        self.assertContains(self.client.get("/pay/"), "Key deposit")
        self.assertContains(self.client.get("/credit/"), "Top up by card")
        self.assertContains(self.client.get("/account/"), "/pay/")


class WebhookTests(CardTestCase):
    def test_paid_by_webhook_before_the_member_comes_back(self):
        self.start("top-up", amount="10")
        self.stripe.pay(self.session_id())
        self.assertEqual(self.webhook("checkout.session.completed", self.session_id()).status_code, 200)
        self.assertEqual(Payment.objects.get().amount_pence, 1000)
        self.assertIn(": paid (payment", StripeEvent.objects.get().outcome)

    def test_a_repeated_event_is_ignored(self):
        self.start("top-up", amount="10")
        self.stripe.pay(self.session_id())
        self.webhook("checkout.session.completed", self.session_id())
        self.webhook("checkout.session.completed", self.session_id())
        self.assertEqual(StripeEvent.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)

    def test_signature(self):
        self.assertEqual(self.webhook("checkout.session.completed", "cs_x", secret="wrong").status_code, 403)
        with override_settings(STRIPE_WEBHOOK_SECRET=""):
            self.assertEqual(self.webhook("checkout.session.completed", "cs_x").status_code, 403)

    def test_expired(self):
        self.start("top-up", amount="10")
        self.stripe.expire(self.session_id())
        self.webhook("checkout.session.expired", self.session_id())
        self.assertEqual(CardCheckout.objects.get().status, "expired")

    def test_other_events_are_logged(self):
        self.webhook("charge.refunded", "ch_1")
        self.assertEqual(StripeEvent.objects.get().outcome, "logged")


class SiteTests(CardTestCase):
    @override_settings(TESTING=False, DEBUG=False)
    def test_without_a_key_card_payments_are_unavailable(self):
        with self.assertLogs("apps.payments.cards", "ERROR"):
            response = self.start("top-up", amount="10")
        self.assertRedirects(response, "/pay/", fetch_redirect_response=False)
        self.assertContains(self.client.get("/pay/"), "Card payments aren&#x27;t available")

    def test_the_fake_page_is_for_development_only(self):
        self.start("top-up", amount="10")
        self.assertEqual(self.client.get(f"/dev/stripe/{self.session_id()}/").status_code, 404)

    @override_settings(DEBUG=True)
    def test_the_fake_page_when_developing(self):
        self.start("top-up", amount="10")
        self.assertContains(self.client.get(f"/dev/stripe/{self.session_id()}/"), "Pay with a test card")
        self.client.post(f"/dev/stripe/{self.session_id()}/", follow=True)
        self.assertEqual(credit_pence(self.member), 1000)

    def test_admin_pages(self):
        admin = Member.objects.create_superuser("a@example.com", "a-good-password", given_name="A", family_name="D")
        self.client.force_login(admin)
        for url in ("/admin/payments/cardcheckout/", "/admin/payments/stripeevent/"):
            self.assertEqual(self.client.get(url).status_code, 200)
