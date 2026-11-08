"""The member's payment pages and the admin (spec 03 §3.1, §3.7-3.9, 04 §4.5)."""

from datetime import timedelta

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from apps.billing import membership
from apps.billing.models import Charge, MandateSetup
from apps.members.models import Member
from apps.payments.models import Payment
from apps.site_settings.models import SiteSettings

from .helpers import BillingTestCase, payer, top_up


class PaymentPageTests(BillingTestCase):
    def login(self, **fields):
        self.member = payer(**fields)
        self.client.force_login(self.member)
        return self.member

    def test_the_page_and_the_account_page_link_to_each_other(self):
        self.login()
        self.assertContains(self.client.get("/account/"), "/account/payment/")
        self.assertContains(self.client.get("/account/payment/"), "Your Direct Debit is set up")

    def test_setting_up_a_direct_debit_through_gocardless(self):
        new = self.login(method=Member.PaymentMethod.NONE, status=Member.Status.SETTING_UP, mandate_id="", day=None)
        response = self.client.post("/account/payment/direct-debit/")
        request_id = MandateSetup.objects.get(member=new).billing_request_id
        self.assertRedirects(response, f"/dev/gocardless/{request_id}/", fetch_redirect_response=False)
        # The member authorises at GoCardless, which sends them back.
        back = self.gc.authorise(request_id)
        self.assertTrue(back.endswith("/account/payment/direct-debit/done/"))
        self.assertRedirects(self.client.get("/account/payment/direct-debit/done/"), "/account/")
        new.refresh_from_db()
        self.assertEqual((new.status, new.payment_method), ("active", "gocardless-variable"))
        self.assertEqual(new.payment_day, membership.payment_day_for(timezone.localdate()))
        self.assertEqual(len(self.gc.payments), 1)  # first month billed straight away
        self.assertEqual(Charge.objects.get(member=new).status, "processing")

    def test_coming_back_before_gocardless_has_finished(self):
        self.login(method=Member.PaymentMethod.NONE, status=Member.Status.SETTING_UP, mandate_id="", day=None)
        self.client.post("/account/payment/direct-debit/")
        response = self.client.get("/account/payment/direct-debit/done/", follow=True)
        self.assertContains(response, "hasn&#x27;t confirmed your Direct Debit yet")
        self.member.refresh_from_db()
        self.assertEqual(self.member.status, "setting-up")

    def test_gocardless_down(self):
        self.login(method=Member.PaymentMethod.NONE, status=Member.Status.SETTING_UP, mandate_id="", day=None)
        self.gc.fail_next = "down"
        response = self.client.post("/account/payment/direct-debit/", follow=True)
        self.assertContains(response, "couldn&#x27;t reach GoCardless")

    def test_paying_from_credit_instead(self):
        new = self.login(method=Member.PaymentMethod.NONE, status=Member.Status.SETTING_UP, mandate_id="", day=None)
        self.assertContains(self.client.get("/account/payment/"), "Pay from my credit")
        self.client.post("/account/payment/credit/")
        new.refresh_from_db()
        self.assertEqual((new.status, new.payment_method), ("active", "balance"))
        self.assertEqual(Charge.objects.get(member=new).status, "due")  # taken from credit tonight

    def test_credit_isnt_offered_to_someone_already_paying(self):
        member = self.login()
        self.assertNotContains(self.client.get("/account/payment/"), "Pay from my credit")
        self.client.post("/account/payment/credit/")
        member.refresh_from_db()
        self.assertEqual(member.payment_method, "gocardless-variable")

    def test_switching_between_direct_debit_and_credit(self):
        member = self.login()
        self.client.post("/account/payment/method/", {"method": "balance"})
        member.refresh_from_db()
        self.assertEqual((member.payment_method, member.mandate_id), ("balance", "MD-TEST1"))  # kept as backup
        self.assertContains(self.client.get("/account/payment/"), "with Direct Debit as a backup")
        self.client.post("/account/payment/method/", {"method": "gocardless-variable"})
        member.refresh_from_db()
        self.assertEqual(member.payment_method, "gocardless-variable")

    def test_standing_order_payers_ask_a_trustee(self):
        member = self.login(method=Member.PaymentMethod.STANDING_ORDER)
        self.client.post("/account/payment/method/", {"method": "balance"})
        response = self.client.get("/account/subscription/", follow=True)
        self.assertContains(response, "ask a trustee")
        member.refresh_from_db()
        self.assertEqual(member.payment_method, "standing-order")

    def test_paying_what_is_owed_from_credit(self):
        member = self.login(method=Member.PaymentMethod.CREDIT)
        charge = Charge.objects.create(member=member, charge_date=timezone.localdate(), status="due")
        response = self.client.post(f"/account/charges/{charge.pk}/pay/", follow=True)
        self.assertContains(response, "Top up your credit first")
        top_up(member, 2500)
        self.client.post(f"/account/charges/{charge.pk}/pay/")
        charge.refresh_from_db()
        self.assertEqual(charge.status, "paid")

    def test_cant_pay_someone_elses_charge(self):
        self.login()
        other = Charge.objects.create(member=payer(), charge_date=timezone.localdate(), status="due")
        self.assertEqual(self.client.post(f"/account/charges/{other.pk}/pay/").status_code, 404)

    def test_leaving_cancels_the_direct_debit(self):
        member = self.login(subscription_expires=timezone.localdate() + timedelta(days=12))
        self.assertContains(self.client.get("/account/leave/"), "cancel your Direct Debit")
        self.client.post("/account/leave/", {"confirm": "on"})
        member.refresh_from_db()
        self.assertEqual((member.status, member.mandate_id), ("leaving", ""))
        self.assertEqual(self.gc.cancelled_mandates, ["MD-TEST1"])
        self.assertIn("you're leaving", mail.outbox[0].subject)
        self.assertContains(self.client.get("/account/"), "Set up your payment again")

    def test_rejoining(self):
        member = self.login(status=Member.Status.LEFT)
        self.assertContains(self.client.get("/account/"), "Rejoin")
        self.client.post("/account/rejoin/")
        member.refresh_from_db()
        self.assertEqual((member.status, member.payment_method), ("setting-up", ""))

    @override_settings(DEBUG=True)
    def test_the_fake_gocardless_pages_when_developing(self):
        new = self.login(method=Member.PaymentMethod.NONE, status=Member.Status.SETTING_UP, mandate_id="", day=None)
        page = self.client.post("/account/payment/direct-debit/", follow=True)
        self.assertContains(page, "Authorise the Direct Debit")
        request_id = MandateSetup.objects.get(member=new).billing_request_id
        self.client.post(f"/dev/gocardless/{request_id}/", follow=True)
        new.refresh_from_db()
        self.assertEqual(new.status, "active")
        (payment_id,) = self.gc.payments
        self.assertContains(self.client.get("/dev/gocardless/payments/"), payment_id)
        self.client.post("/dev/gocardless/payments/", {"payment": payment_id, "action": "confirmed"})
        self.assertEqual(Charge.objects.get(member=new).status, "paid")

    @override_settings(TESTING=False, DEBUG=False)
    def test_a_site_without_gocardless_says_so(self):
        self.login(method=Member.PaymentMethod.NONE, status=Member.Status.SETTING_UP, mandate_id="", day=None)
        with self.assertLogs("apps.billing.gocardless", "ERROR"):
            response = self.client.post("/account/payment/direct-debit/", follow=True)
        self.assertContains(response, "couldn&#x27;t reach GoCardless")

    def test_the_fake_gocardless_pages_are_for_development_only(self):
        self.login()
        self.assertEqual(self.client.get("/dev/gocardless/payments/").status_code, 404)


class AdminTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.admin = Member.objects.create_superuser(
            "admin@example.com", "a-good-password", given_name="A", family_name="Dmin"
        )
        self.client.force_login(self.admin)

    def test_record_a_cash_subscription(self):
        member = payer(Member.PaymentMethod.CASH)
        response = self.client.post(
            "/admin/payments/payment/record/", {"member": member.pk, "what": "subscription", "paid_how": "cash"}
        )
        self.assertEqual(response.status_code, 302)
        charge = Charge.objects.get(member=member)
        member.refresh_from_db()
        self.assertEqual((charge.status, charge.amount_pence), ("paid", 2500))
        self.assertEqual(member.subscription_expires, membership.add_month(charge.paid_on))
        self.assertEqual(charge.payments.get().source, Payment.Source.CASH)

    def test_a_cash_subscription_pays_the_oldest_unpaid_charge(self):
        member = payer(Member.PaymentMethod.CASH)
        old = Charge.objects.create(member=member, charge_date=timezone.localdate() - timedelta(days=20), status="due")
        self.client.post(
            "/admin/payments/payment/record/", {"member": member.pk, "what": "subscription", "paid_how": "cash"}
        )
        old.refresh_from_db()
        self.assertEqual(old.status, "paid")
        self.assertEqual(Charge.objects.count(), 1)

    def test_member_page_shows_charges_and_billing_fields(self):
        member = payer()
        Charge.objects.create(member=member, charge_date=timezone.localdate(), status="due")
        response = self.client.get(f"/admin/members/member/{member.pk}/change/")
        self.assertContains(response, "Charges (newest first)")
        self.assertContains(response, "MD-TEST1")

    def test_admin_minimum_subscription(self):
        SiteSettings.load()
        member = payer()
        page = self.client.get(f"/admin/members/member/{member.pk}/change/")
        form = page.context["adminform"].form
        data = {k: v for k, v in form.initial.items() if v is not None and not isinstance(v, list)}
        data.update(monthly_subscription_pence=400, status="active", payment_method="gocardless-variable")
        response = self.client.post(f"/admin/members/member/{member.pk}/change/", data)
        self.assertContains(response, "At least 500p")

    def test_retry_a_failed_direct_debit(self):
        member = payer()
        charge = Charge.objects.create(
            member=member, charge_date=timezone.localdate(), status="failed", amount_pence=2500
        )
        Payment.objects.create(
            member=member,
            reason="subscription",
            source="gocardless-variable",
            status="failed",
            amount_pence=2500,
            source_id="PM-X",
            charge=charge,
        )
        self.gc.payments["PM-X"] = {"status": "failed"}
        self.client.post("/admin/billing/charge/", {"action": "retry_direct_debit", "_selected_action": [charge.pk]})
        charge.refresh_from_db()
        self.assertEqual(charge.status, "processing")
        self.assertEqual(self.gc.retried, ["PM-X"])

    def test_gocardless_events_page(self):
        self.assertEqual(self.client.get("/admin/billing/gocardlessevent/").status_code, 200)
        self.assertEqual(self.client.get("/admin/billing/charge/").status_code, 200)
