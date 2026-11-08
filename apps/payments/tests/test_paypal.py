"""PayPal subscriptions and credit top-ups (spec 04 §4.4)."""

from io import BytesIO
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlparse

from django.contrib.auth.models import Group
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.billing import billing, membership
from apps.billing.models import Charge
from apps.billing.tests.helpers import payer
from apps.equipment.tests.helpers import member as make_member
from apps.members.models import Member
from apps.payments import paypal
from apps.payments.credit import credit_pence
from apps.payments.models import Payment, PayPalNotification

FAKE = paypal.FAKE_ACCOUNT


@override_settings(PAYPAL_ACCOUNT_EMAIL="", PAYPAL_OLD_ACCOUNT_EMAILS=[], PAYPAL_SANDBOX=True)
class PayPalTestCase(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.finance = make_member()
        Group.objects.get_or_create(name="finance")[0].user_set.add(self.finance)
        self._txn = 0

    def txn(self):
        self._txn += 1
        return f"TXN{self._txn:06d}"

    def ipn(self, **data):
        """PayPal posting a notification to BBMS."""
        data = {"receiver_email": FAKE, "mc_currency": "GBP", "charset": "UTF-8", **data}
        return self.client.post("/paypal-ipn", urlencode(data), content_type="application/x-www-form-urlencoded")

    def pays(self, who, amount="25.00", **data):
        defaults = {
            "txn_type": "subscr_payment",
            "txn_id": self.txn(),
            "payment_status": "Completed",
            "mc_gross": amount,
            "mc_fee": "1.03",
            "payer_email": who.email,
            "subscr_id": "I-SUB1",
        }
        return self.ipn(**{**defaults, **data})

    def joiner(self, **fields):
        return make_member(status=Member.Status.SETTING_UP, monthly_subscription_pence=2500, **fields)

    def paypal_payer(self, **fields):
        return payer(Member.PaymentMethod.PAYPAL, day=self.today.day, paypal_subscription_id="I-SUB1", **fields)


class JoiningTests(PayPalTestCase):
    def test_subscribe_from_the_payment_page(self):
        joiner = self.joiner()
        self.client.force_login(joiner)
        self.assertContains(self.client.get("/account/payment/"), "Subscribe with PayPal")
        response = self.client.post("/account/payment/paypal/")
        url = urlparse(response["Location"])
        params = {name: values[0] for name, values in parse_qs(url.query).items()}
        self.assertEqual(url.path, "/dev/paypal/")
        self.assertEqual(
            (params["cmd"], params["a3"], params["t3"], params["business"]),
            ("_xclick-subscriptions", "25.00", "M", FAKE),
        )
        self.assertEqual(paypal.member_id_from(params["custom"]), joiner.pk)
        self.assertTrue(params["notify_url"].endswith("/paypal-ipn"))

        # PayPal says the member subscribed, then that the first payment came in.
        self.ipn(txn_type="subscr_signup", subscr_id="I-NEW", custom=params["custom"], payer_email="other@example.com")
        joiner.refresh_from_db()
        self.assertEqual((joiner.status, joiner.payment_method), ("active", "paypal"))
        self.assertEqual(
            (joiner.paypal_subscription_id, joiner.payment_day), ("I-NEW", membership.payment_day_for(self.today))
        )
        charge = Charge.objects.get(member=joiner)
        self.assertEqual(charge.status, "due")
        self.pays(joiner, subscr_id="I-NEW", payer_email="other@example.com")
        charge.refresh_from_db()
        joiner.refresh_from_db()
        self.assertEqual(charge.status, "paid")
        payment = charge.payments.get()
        self.assertEqual((payment.source, payment.amount_pence, payment.fee_pence), ("paypal", 2500, 103))
        self.assertEqual(joiner.subscription_expires, membership.add_month(self.today))
        self.assertIn("welcome", mail.outbox[0].subject.lower())

    def test_the_first_payment_sets_them_up_without_a_signup(self):
        joiner = self.joiner()
        self.pays(joiner, custom=paypal.member_reference(joiner))
        joiner.refresh_from_db()
        self.assertEqual(
            (joiner.status, joiner.payment_method, joiner.paypal_subscription_id), ("active", "paypal", "I-SUB1")
        )
        self.assertEqual(Charge.objects.get(member=joiner).status, "paid")

    def test_not_offered_to_members_paying_another_way(self):
        dd = payer()
        self.client.force_login(dd)
        self.assertNotContains(self.client.get("/account/payment/"), "Subscribe with PayPal")
        self.client.post("/account/payment/paypal/")
        self.assertFalse(PayPalNotification.objects.exists())

    def test_a_paypal_payer_can_start_a_new_subscription(self):
        who = self.paypal_payer()
        self.client.force_login(who)
        self.assertContains(self.client.get("/account/payment/"), "Start a new PayPal subscription for £25.00")
        self.ipn(txn_type="subscr_signup", subscr_id="I-SUB2", payer_email=who.email)
        who.refresh_from_db()
        self.assertEqual(who.paypal_subscription_id, "I-SUB2")
        # Cancelling the old one afterwards changes nothing.
        self.ipn(txn_type="subscr_cancel", subscr_id="I-SUB1", payer_email=who.email)
        who.refresh_from_db()
        self.assertEqual(who.status, "active")


class MonthlyTests(PayPalTestCase):
    def test_a_payment_before_the_charge_waits_in_credit(self):
        who = self.paypal_payer()
        self.pays(who)
        self.assertEqual(credit_pence(who), 2500)
        Charge.objects.create(member=who, charge_date=self.today, status="pending")
        self.assertEqual(billing.bill_member(who, self.today), "nothing due")
        billing.make_due(self.today)
        self.assertEqual(billing.bill_member(who, self.today), "paid from credit")
        self.assertEqual(credit_pence(who), 0)

    def test_a_charge_waits_for_paypal_without_an_email(self):
        who = self.paypal_payer()
        charge = Charge.objects.create(member=who, charge_date=self.today, status="due")
        self.assertEqual(billing.bill_member(who, self.today), "waiting for paypal")
        self.assertEqual(mail.outbox, [])
        self.pays(who, amount="20.00")  # an old subscription for less: the charge takes what PayPal took
        charge.refresh_from_db()
        self.assertEqual((charge.status, charge.amount_pence), ("paid", 2000))

    def test_matched_by_subscription_then_paypal_email(self):
        who = self.paypal_payer(paypal_email="pp@example.com")
        self.pays(who, payer_email="someone-else@example.com")  # the subscription id matches
        self.pays(who, subscr_id="I-OTHER", payer_email="PP@example.com")
        self.assertEqual(Payment.objects.filter(member=who).count(), 2)

    def test_repeats_and_status_changes_are_recorded_once(self):
        who = self.paypal_payer()
        self.pays(who, txn_id="T1", payment_status="Pending")
        self.assertEqual(credit_pence(who), 0)
        self.pays(who, txn_id="T1")
        self.pays(who, txn_id="T1")
        self.assertEqual(Payment.objects.filter(source="paypal").count(), 1)
        self.assertEqual(PayPalNotification.objects.count(), 2)

    def test_failed_payment_emails_the_member(self):
        who = self.paypal_payer()
        self.ipn(txn_type="subscr_failed", subscr_id="I-SUB1", payer_email=who.email)
        self.assertIn("didn't go through", mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].to, [who.email])


class UnusualPaymentTests(PayPalTestCase):
    def test_unknown_payer_is_a_donation(self):
        self.ipn(
            txn_type="subscr_payment",
            txn_id="T9",
            payment_status="Completed",
            mc_gross="10.00",
            payer_email="stranger@example.com",
        )
        payment = Payment.objects.get()
        self.assertEqual((payment.member, payment.reason), (None, "donation"))
        self.assertEqual({m.to[0] for m in mail.outbox}, {"stranger@example.com", self.finance.email})

    def test_finance_assigns_a_donation_to_a_member(self):
        self.ipn(
            txn_type="web_accept", txn_id="T9", payment_status="Completed", mc_gross="10.00", payer_email="s@x.org"
        )
        payment = Payment.objects.get()
        who = make_member()
        admin = Member.objects.create_superuser("a@example.com", "a-good-password", given_name="A", family_name="D")
        self.client.force_login(admin)
        url = f"/admin/payments/payment/{payment.pk}/change/"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url, {"member": who.pk, "note": ""})
        payment.refresh_from_db()
        self.assertEqual((payment.member, payment.reason), (who, "balance"))
        self.assertEqual(credit_pence(who), 1000)

    def test_a_member_who_has_left_isnt_rejoined(self):
        who = self.paypal_payer(status=Member.Status.LEFT)
        self.pays(who)
        who.refresh_from_db()
        self.assertEqual(who.status, "left")
        self.assertEqual(credit_pence(who), 2500)
        self.assertEqual(mail.outbox[0].to, [self.finance.email])

    def test_wrong_account_or_currency_is_kept_for_a_person(self):
        who = self.paypal_payer()
        self.pays(who, receiver_email="someone@example.com")
        self.pays(who, mc_currency="EUR")
        self.assertFalse(Payment.objects.exists())
        errors = sorted(PayPalNotification.objects.values_list("error", flat=True))
        self.assertEqual(errors[1], "Not in pounds (EUR)")
        self.assertEqual(errors[0], "Not for the space's PayPal account (it's for someone@example.com)")

    def test_old_account_payments_count_during_the_switch(self):
        who = self.paypal_payer()
        self.pays(who, receiver_email="Old@Example.com")
        notification = PayPalNotification.objects.get()
        self.assertTrue(notification.error)
        with override_settings(PAYPAL_OLD_ACCOUNT_EMAILS=["old@example.com"]):
            admin = Member.objects.create_superuser("a@example.com", "a-good-password", given_name="A", family_name="D")
            self.client.force_login(admin)
            self.client.post(
                "/admin/payments/paypalnotification/",
                {"action": "try_again", "_selected_action": [notification.pk]},
            )
        notification.refresh_from_db()
        self.assertEqual(notification.error, "")
        self.assertEqual(credit_pence(who), 2500)


class TopUpTests(PayPalTestCase):
    def test_top_up(self):
        who = payer()
        self.client.force_login(who)
        self.assertContains(self.client.get("/credit/"), "Top up by PayPal")
        response = self.client.post("/pay/paypal/", {"item": "top-up", "amount": "20", "back": "credit"})
        params = parse_qs(urlparse(response["Location"]).query)
        self.assertEqual((params["cmd"], params["amount"]), (["_xclick"], ["20.00"]))
        self.ipn(
            txn_type="web_accept",
            txn_id="T1",
            payment_status="Completed",
            mc_gross="20.00",
            mc_fee="0.88",
            custom=params["custom"][0],
            payer_email="whoever@example.com",
        )
        self.assertEqual(credit_pence(who), 2000)
        self.assertEqual(Payment.objects.get().fee_pence, 88)

    def test_limits(self):
        self.client.force_login(payer())
        response = self.client.post("/pay/paypal/", {"item": "top-up", "amount": "1"})
        self.assertRedirects(response, "/pay/", fetch_redirect_response=False)

    def test_what_paypal_took_is_what_counts(self):
        who = payer()
        self.ipn(
            txn_type="web_accept",
            txn_id="T1",
            payment_status="Completed",
            mc_gross="0.01",
            custom=paypal.member_reference(who),
        )
        self.assertEqual(credit_pence(who), 1)


class RefundTests(PayPalTestCase):
    def test_refunded_top_up(self):
        who = payer()
        self.ipn(
            txn_type="web_accept", txn_id="T1", payment_status="Completed", mc_gross="20.00", payer_email=who.email
        )
        self.ipn(txn_id="T2", parent_txn_id="T1", payment_status="Refunded", mc_gross="-20.00", payer_email=who.email)
        self.assertEqual(credit_pence(who), 0)
        correction = Payment.objects.get(source_id="T2")
        self.assertEqual(correction.corrects, Payment.objects.get(source_id="T1"))
        self.assertIn("refunded", mail.outbox[-1].subject)

    def test_reversed_subscription_and_the_reversal_cancelled(self):
        who = self.paypal_payer()
        charge = Charge.objects.create(member=who, charge_date=self.today, status="due")
        self.pays(who, txn_id="T1")
        self.ipn(txn_id="T2", parent_txn_id="T1", payment_status="Reversed", mc_gross="-25.00")
        charge.refresh_from_db()
        self.assertEqual(charge.status, "failed")
        self.ipn(txn_id="T3", parent_txn_id="T1", payment_status="Canceled_Reversal", mc_gross="25.00")
        charge.refresh_from_db()
        self.assertEqual(charge.status, "paid")

    def test_refund_of_an_unknown_payment(self):
        self.ipn(txn_id="T2", parent_txn_id="NOPE", payment_status="Refunded", mc_gross="-5.00")
        self.assertIn("doesn't have", PayPalNotification.objects.get().error)


class LeavingTests(PayPalTestCase):
    def test_cancelled_at_paypal(self):
        who = self.paypal_payer()
        self.ipn(txn_type="subscr_cancel", subscr_id="I-SUB1", payer_email=who.email)
        who.refresh_from_db()
        self.assertEqual(who.status, "leaving")
        self.assertNotIn("cancel your PayPal subscription", mail.outbox[-1].body)

    def test_leaving_in_bbms_asks_them_to_cancel_at_paypal(self):
        who = self.paypal_payer()
        self.client.force_login(who)
        self.assertContains(self.client.get("/account/leave/"), "We can't cancel your PayPal subscription")
        membership.start_leaving(who)
        self.assertIn("cancel your PayPal subscription", mail.outbox[-1].body)

    def test_switching_to_credit(self):
        who = self.paypal_payer()
        self.client.force_login(who)
        response = self.client.post("/account/payment/method/", {"method": "balance"}, follow=True)
        self.assertContains(response, "cancel your PayPal subscription")
        who.refresh_from_db()
        self.assertEqual(who.payment_method, "balance")


class VerificationTests(PayPalTestCase):
    def answer(self, body):
        response = mock.MagicMock()
        response.__enter__.return_value = BytesIO(body)
        return response

    @override_settings(PAYPAL_ACCOUNT_EMAIL="Space@Example.com")
    def test_checked_with_paypal(self):
        who = payer()
        with mock.patch("urllib.request.urlopen", return_value=self.answer(b"INVALID")) as urlopen:
            response = self.ipn(
                txn_type="web_accept",
                txn_id="T1",
                payment_status="Completed",
                mc_gross="5.00",
                payer_email=who.email,
                receiver_email="space@example.com",
            )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PayPalNotification.objects.exists())
        sent = urlopen.call_args.args[0]
        self.assertEqual(sent.full_url, "https://ipnpb.sandbox.paypal.com/cgi-bin/webscr")
        self.assertTrue(sent.data.startswith(b"cmd=_notify-validate&receiver_email="))
        with mock.patch("urllib.request.urlopen", return_value=self.answer(b"VERIFIED")):
            self.ipn(
                txn_type="web_accept",
                txn_id="T1",
                payment_status="Completed",
                mc_gross="5.00",
                payer_email=who.email,
                receiver_email="space@example.com",
            )
        self.assertEqual(credit_pence(who), 500)
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertEqual(self.ipn(txn_id="T2").status_code, 503)

    @override_settings(TESTING=False, DEBUG=False)
    def test_refused_without_an_account(self):
        self.assertEqual(self.ipn(txn_id="T1").status_code, 403)
        self.client.force_login(payer())
        self.assertNotContains(self.client.get("/credit/"), "Top up by PayPal")

    def test_character_sets(self):
        body = b"charset=windows-1252&first_name=Ren%E9e"
        self.assertEqual(paypal.parse(body)["first_name"], "Renée")


class PageTests(PayPalTestCase):
    def test_paypal_email_on_the_details_page(self):
        who = payer()
        other = make_member(paypal_email="taken@example.com")
        self.client.force_login(who)
        details = {"given_name": "Mo", "family_name": "M", "phone": "01234567890", "emergency_contact": "Someone"}
        response = self.client.post("/account/details/", {**details, "paypal_email": "TAKEN@example.com"})
        self.assertContains(response, "belongs to another account")
        self.client.post("/account/details/", {**details, "paypal_email": "Mine@Example.com"})
        who.refresh_from_db()
        self.assertEqual(who.paypal_email, "mine@example.com")
        self.assertNotEqual(other.pk, who.pk)

    def test_fake_paypal_is_for_development_only(self):
        self.assertEqual(self.client.get("/dev/paypal/tools/").status_code, 404)

    @override_settings(DEBUG=True)
    def test_fake_paypal(self):
        joiner = self.joiner()
        self.client.force_login(joiner)
        url = self.client.post("/account/payment/paypal/")["Location"]
        self.assertContains(self.client.get(url), "Subscribe with PayPal")
        self.client.post(url)
        joiner.refresh_from_db()
        self.assertEqual((joiner.status, joiner.payment_method), ("active", "paypal"))
        tools = self.client.get("/dev/paypal/tools/")
        self.assertContains(tools, joiner.paypal_subscription_id)
        self.client.post("/dev/paypal/tools/", {"subscr_id": joiner.paypal_subscription_id, "action": "payment"})
        self.assertEqual(credit_pence(joiner), 2500)  # no charge waiting: to credit
        payment = Payment.objects.filter(member=joiner).latest("pk")
        self.client.post("/dev/paypal/tools/", {"payment": payment.pk, "action": "refund"})
        self.assertEqual(credit_pence(joiner), 0)

    def test_admin_pages(self):
        admin = Member.objects.create_superuser("a@example.com", "a-good-password", given_name="A", family_name="D")
        self.client.force_login(admin)
        self.pays(payer())
        self.assertEqual(self.client.get("/admin/payments/paypalnotification/").status_code, 200)
        notification = PayPalNotification.objects.get()
        url = f"/admin/payments/paypalnotification/{notification.pk}/change/"
        self.assertEqual(self.client.get(url).status_code, 200)
