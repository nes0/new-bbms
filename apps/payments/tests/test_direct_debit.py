"""One-off Direct Debit payments from the Pay page (spec 04 §4.1, §4.2)."""

from django.core import mail
from django.utils import timezone

from apps.billing.models import Charge
from apps.billing.tests.helpers import payer
from apps.billing.tests.test_webhooks import WebhookTestCase, event
from apps.equipment.models import Equipment, Induction
from apps.members.models import Member
from apps.payments import checkout
from apps.payments.credit import credit_pence
from apps.payments.models import Payment


class DirectDebitPayTests(WebhookTestCase):
    def setUp(self):
        super().setUp()
        self.member = payer(trusted=True)
        self.client.force_login(self.member)

    def pay(self, item, **data):
        return self.client.post("/pay/direct-debit/", {"item": item, **data})

    def only_payment(self):
        ((payment_id, sent),) = self.gc.payments.items()
        return payment_id, sent

    def test_the_buttons_show_only_with_a_mandate(self):
        self.assertContains(self.client.get("/pay/"), "Pay by Direct Debit")
        self.assertContains(self.client.get("/credit/"), "Top up by Direct Debit")
        self.member.mandate_id = ""
        self.member.save()
        page = self.client.get("/pay/")
        self.assertNotContains(page, "Pay by Direct Debit")
        self.assertContains(page, "Set up a Direct Debit")
        response = self.pay("key-deposit")
        self.assertFalse(self.gc.payments)
        self.assertEqual(response.status_code, 302)

    def test_key_deposit_by_direct_debit(self):
        response = self.pay("key-deposit", amount="1")  # the amount is BBMS's, whatever the browser sends
        self.assertRedirects(response, "/pay/", fetch_redirect_response=False)
        payment_id, sent = self.only_payment()
        self.assertEqual((sent["mandate"], sent["amount"]), ("MD-TEST1", 1500))
        payment = Payment.objects.get(source_id=payment_id)
        self.assertEqual((payment.reason, payment.status), ("key-deposit", "pending"))
        self.assertNotIn("key-deposit", [i.key for i in checkout.payables(self.member)])  # pending counts
        self.send(event("payments", "confirmed", {"payment": payment_id}))
        payment.refresh_from_db()
        self.assertEqual(payment.status, "paid")

    def test_asking_twice_on_the_same_day_takes_it_once(self):
        self.pay("storage-box")
        Payment.objects.all().delete()  # as if the first request's page never loaded and the member clicked again
        checkout.pay_by_direct_debit(
            self.member, purpose="storage-box", amount_pence=1500, label="Box fee", key="storage-box"
        )
        checkout.pay_by_direct_debit(
            self.member, purpose="storage-box", amount_pence=1500, label="Box fee", key="storage-box"
        )
        self.assertEqual(len(self.gc.payments), 1)
        self.assertEqual(Payment.objects.count(), 1)

    def test_top_up_counts_at_once_and_is_taken_back_if_it_fails(self):
        self.assertRedirects(self.pay("top-up", amount="20", back="credit"), "/credit/", fetch_redirect_response=False)
        self.assertEqual(credit_pence(self.member), 2000)
        payment_id, _ = self.only_payment()
        self.send(event("payments", "failed", {"payment": payment_id}, {"cause": "insufficient_funds"}))
        self.assertEqual(credit_pence(self.member), 0)
        self.assertIn("We won't try again", mail.outbox[-1].body)  # one-off payments aren't retried
        self.assertIn("credit top-up", mail.outbox[-1].body)

    def test_top_up_limits(self):
        self.pay("top-up", amount="1")
        self.assertFalse(self.gc.payments)

    def test_an_induction_fee_that_fails_is_unpaid_again(self):
        laser = Equipment.objects.create(name="Laser", slug="laser", requires_induction=True, induction_fee_pence=1000)
        self.pay(f"induction:{laser.pk}")
        induction = Induction.objects.get(member=self.member)
        self.assertTrue(induction.paid)
        payment_id, _ = self.only_payment()
        self.send(event("payments", "failed", {"payment": payment_id}, {"cause": "bank_account_closed"}))
        induction.refresh_from_db()
        self.assertFalse(induction.paid)

    def test_a_failed_charge_paid_again_by_direct_debit(self):
        charge = Charge.objects.create(
            member=self.member, charge_date=timezone.localdate(), status="failed", amount_pence=2500
        )
        self.assertContains(self.client.get("/account/payment/"), "Pay by Direct Debit")
        self.assertRedirects(self.pay(f"charge:{charge.pk}"), "/account/payment/", fetch_redirect_response=False)
        charge.refresh_from_db()
        self.assertEqual(charge.status, "processing")
        payment_id, sent = self.only_payment()
        self.assertEqual(sent["amount"], 2500)
        self.send(event("payments", "confirmed", {"payment": payment_id}))
        charge.refresh_from_db()
        self.assertEqual(charge.status, "paid")

    def test_gocardless_down(self):
        self.gc.fail_next = "down"
        response = self.client.post("/pay/direct-debit/", {"item": "storage-box"}, follow=True)
        self.assertContains(response, "couldn&#x27;t reach GoCardless")
        self.assertFalse(Payment.objects.exists())

    def test_not_for_members_who_cant_pay_that(self):
        self.member.status = Member.Status.SUSPENDED
        self.member.save()
        response = self.client.post("/pay/direct-debit/", {"item": "storage-box"}, follow=True)
        self.assertContains(response, "doesn&#x27;t need paying any more")
