"""The finance pages (spec 04 §4.8) and membership stats."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.access.models import AccessEvent
from apps.billing.models import Charge
from apps.equipment.models import Induction
from apps.equipment.tests.helpers import laser, member
from apps.finance.tests.test_statements import finance_member
from apps.members.models import Member
from apps.payments import services
from apps.payments.credit import credit_pence
from apps.payments.models import Payment

BALANCE, CASH, STRIPE = Payment.Reason.BALANCE, Payment.Source.CASH, Payment.Source.STRIPE


class PaymentsListTests(TestCase):
    def setUp(self):
        self.client.force_login(finance_member())
        self.kim = member(given_name="Kim", family_name="Lee")
        self.old = services.record_payment(
            self.kim, BALANCE, CASH, 500, created_at=timezone.now() - timedelta(days=70), reference="=SUM(A1)"
        )
        self.new = services.record_payment(self.kim, Payment.Reason.SUBSCRIPTION, STRIPE, 2000, fee_pence=68)
        self.stranger = services.record_payment(None, Payment.Reason.DONATION, Payment.Source.PAYPAL, 1000)

    def test_filters_and_totals(self):
        page = self.client.get("/finance/payments/")
        self.assertEqual(list(page.context["page"]), [self.stranger, self.new, self.old])
        self.assertContains(page, "3 payments: £35.00")
        self.assertContains(page, "fees £0.68, net £34.32")
        month = timezone.localdate().strftime("%Y-%m")
        page = self.client.get(f"/finance/payments/?month={month}&source=stripe")
        self.assertEqual(list(page.context["page"]), [self.new])
        page = self.client.get(f"/finance/payments/?member=Kim+Lee+(%23{self.kim.pk})&sort=smallest")
        self.assertEqual(list(page.context["page"]), [self.old, self.new])
        self.assertEqual(list(self.client.get("/finance/payments/?member=nobody").context["page"]), [])
        self.assertEqual(list(self.client.get("/finance/payments/?unmatched=1").context["page"]), [self.stranger])
        start = (timezone.localdate() - timedelta(days=80)).isoformat()
        end = (timezone.localdate() - timedelta(days=60)).isoformat()
        self.assertEqual(
            list(self.client.get(f"/finance/payments/?start={start}&end={end}").context["page"]), [self.old]
        )

    def test_csv(self):
        response = self.client.get("/finance/payments/?format=csv&sort=oldest")
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        lines = response.content.decode().splitlines()
        self.assertEqual(lines[0], "Date,Member,Member number,For,Paid by,Status,Amount,Fee,Reference")
        self.assertIn(f"Kim Lee,{self.kim.pk},Credit top-up,Cash,Paid,5.00,0.00,'=SUM(A1)", lines[1])
        self.assertEqual(len(lines), 4)


class PutRightTests(TestCase):
    def setUp(self):
        self.fin = finance_member()
        self.client.force_login(self.fin)
        self.kim = member(given_name="Kim", family_name="Lee")

    def post(self, payment, change, **data):
        return self.client.post(f"/finance/payments/{payment.pk}/put-right/", {"change": change, **data}, follow=True)

    def test_assign_unmatched_money(self):
        stranger = services.record_payment(None, Payment.Reason.DONATION, Payment.Source.PAYPAL, 1000)
        self.assertContains(self.client.get(f"/finance/payments/{stranger.pk}/"), "Whose money is this?")
        self.assertContains(self.post(stranger, "assign", member="nobody"), "Please choose the member")
        self.post(stranger, "assign", member=f"Kim Lee (#{self.kim.pk})")
        stranger.refresh_from_db()
        self.assertEqual((stranger.member, stranger.reason), (self.kim, "balance"))
        self.assertEqual(credit_pence(self.kim), 1000)
        self.assertContains(self.post(stranger, "assign", member=str(self.fin.pk)), "already belongs to a member")

    def test_refund_an_induction_fee_before_training(self):
        machine = laser(induction_fee_pence=2500)
        paid = services.pay_induction(self.kim, machine, STRIPE)
        self.assertContains(self.client.get(f"/finance/payments/{paid.pk}/"), "Refund £25.00 to their credit")
        self.post(paid, "refund", note="Course cancelled")
        self.assertEqual(credit_pence(self.kim), 2500)
        induction = Induction.objects.get(member=self.kim)
        self.assertEqual((induction.paid, induction.payment), (False, None))
        rows = list(paid.corrections.order_by("pk").values_list("reason", "source", "amount_pence", "note"))
        self.assertEqual(
            rows, [("induction", "stripe", -2500, "Course cancelled"), ("balance", "stripe", 2500, "Course cancelled")]
        )
        self.assertContains(self.post(paid, "refund"), "already been put right")
        self.assertEqual(credit_pence(self.kim), 2500)
        # Paid from credit: reversing it is the refund.
        from_credit = services.pay_induction(self.kim, machine, Payment.Source.BALANCE)
        self.assertEqual(credit_pence(self.kim), 0)
        Induction.objects.filter(member=self.kim).update(paid=True, payment=from_credit)
        self.post(from_credit, "refund")
        self.assertEqual(credit_pence(self.kim), 2500)

    def test_no_refund_after_training(self):
        machine = laser(induction_fee_pence=2500)
        paid = services.pay_induction(self.kim, machine, STRIPE)
        Induction.objects.filter(member=self.kim).update(trained_at=timezone.now())
        self.assertContains(self.post(paid, "refund"), "had the induction")
        self.assertEqual(credit_pence(self.kim), 0)

    def test_cancel_cash_recorded_by_mistake(self):
        cash = services.record_payment(self.kim, BALANCE, CASH, 700)
        card = services.record_payment(self.kim, BALANCE, STRIPE, 300)
        self.assertNotContains(self.client.get(f"/finance/payments/{card.pk}/"), "Cancel this cash payment")
        self.post(card, "cancel")
        self.assertEqual(credit_pence(self.kim), 1000)
        self.post(cash, "cancel")
        self.assertEqual(credit_pence(self.kim), 300)
        self.assertTrue(Payment.objects.filter(pk=cash.pk).exists())  # the history keeps both

    def test_only_those_who_manage(self):
        viewer = member()
        from django.contrib.auth.models import Permission

        viewer.user_permissions.add(Permission.objects.get(codename="see_finance"))
        cash = services.record_payment(self.kim, BALANCE, CASH, 700)
        self.client.force_login(viewer)
        self.assertNotContains(self.client.get(f"/finance/payments/{cash.pk}/"), "Cancel this cash payment")
        self.assertEqual(
            self.client.post(f"/finance/payments/{cash.pk}/put-right/", {"change": "cancel"}).status_code, 403
        )
        self.assertEqual(credit_pence(self.kim), 700)


class OverviewTests(TestCase):
    def test_figures(self):
        kim, sam = member(), member()
        services.record_payment(kim, BALANCE, CASH, 1000)
        services.pay_from_credit(kim, Payment.Reason.COSTS, 400)
        services.record_payment(sam, Payment.Reason.KEY_DEPOSIT, CASH, 1500)
        services.record_payment(sam, Payment.Reason.STORAGE_BOX, CASH, 1500)
        services.record_payment(None, Payment.Reason.DONATION, Payment.Source.PAYPAL, 100)
        self.client.force_login(finance_member())
        page = self.client.get("/finance/")
        figures = page.context["figures"]
        self.assertEqual(
            (figures["credit_held"], figures["credit_paid_in"], figures["credit_paid_out"]), (600, 1000, 400)
        )
        self.assertEqual((figures["key_deposits"], figures["box_fees"]), (1500, 1500))
        self.assertContains(page, "1 payment nobody could match to a member")

    def test_charges_list(self):
        kim = member(given_name="Kim", family_name="Lee")
        Charge.objects.create(
            member=kim, charge_date=timezone.localdate(), status=Charge.Status.FAILED, amount_pence=1500
        )
        Charge.objects.create(
            member=kim, charge_date=timezone.localdate() - timedelta(days=40), status=Charge.Status.PAID
        )
        self.client.force_login(finance_member())
        page = self.client.get("/finance/charges/?status=failed")
        self.assertEqual(page.context["page"].paginator.count, 1)
        self.assertContains(page, "Kim Lee")
        self.assertContains(page, "£15.00")


class StatsTests(TestCase):
    def test_any_member_sees_stats(self):
        kim = member(payment_method=Member.PaymentMethod.STANDING_ORDER, monthly_subscription_pence=1500)
        member(payment_method=Member.PaymentMethod.DIRECT_DEBIT, monthly_subscription_pence=2200)
        member(payment_method=Member.PaymentMethod.DIRECT_DEBIT, monthly_subscription_pence=2000)
        member(status=Member.Status.LEFT, payment_method=Member.PaymentMethod.DIRECT_DEBIT)
        AccessEvent.objects.create(member=kim, kind=AccessEvent.Kind.ENTRY, occurred_at=timezone.now())
        self.client.force_login(kim)
        page = self.client.get("/stats/")
        self.assertEqual(page.context["members"], 3)
        self.assertEqual(page.context["by_method"][0], {"label": "Direct Debit", "count": 2})
        self.assertEqual([row["label"] for row in page.context["spread"]], ["£15", "£20"])
        self.assertEqual(page.context["average"], 1900)
        self.assertEqual(page.context["came_in_month"], 1)
        self.assertNotContains(page, "/finance/payments/")
        self.client.force_login(member(status=Member.Status.LEFT))
        self.assertEqual(self.client.get("/stats/").status_code, 404)
