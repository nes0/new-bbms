"""Recording payments in the admin, and the member's credit page (spec 04 §4.5, §4.7)."""

from django.test import TestCase
from django.urls import reverse

from apps.equipment.models import Equipment, Induction
from apps.equipment.tests.helpers import member
from apps.members.models import Member
from apps.payments import services
from apps.payments.credit import credit_pence
from apps.payments.models import Payment, WithdrawalRequest

RECORD = "/admin/payments/payment/record/"


class RecordPaymentTests(TestCase):
    def setUp(self):
        self.admin = Member.objects.create_superuser(
            "admin@example.com", "a-good-password", given_name="A", family_name="D"
        )
        self.client.force_login(self.admin)
        self.who = member()

    def record(self, **data):
        return self.client.post(RECORD, {"member": self.who.pk, **data}, follow=True)

    def test_cash_top_up(self):
        response = self.record(what="topup", amount="12.50")
        self.assertContains(response, "Their credit is now £12.50")
        payment = Payment.objects.get()
        self.assertEqual((payment.reason, payment.source, payment.created_by), ("balance", "cash", self.admin))

    def test_withdrawal_needs_the_credit(self):
        response = self.record(what="withdrawal", amount="5")
        self.assertContains(response, "enough to pay £5.00")
        services.record_payment(self.who, Payment.Reason.BALANCE, Payment.Source.CASH, 800)
        self.record(what="withdrawal", amount="5")
        self.assertEqual(credit_pence(self.who), 300)

    def test_fixed_prices_come_from_bbms_not_the_form(self):
        response = self.record(what="key-deposit", paid_how="cash", amount="1")
        self.assertContains(response, "Leave this empty")
        self.record(what="key-deposit", paid_how="cash")
        self.assertEqual(Payment.objects.get().amount_pence, 1500)

    def test_induction_fee_uses_the_machines_fee(self):
        lathe = Equipment.objects.create(name="Lathe", slug="lathe", requires_induction=True, induction_fee_pence=2500)
        self.record(what="induction", paid_how="manual", equipment=lathe.pk)
        self.assertEqual(Payment.objects.get().amount_pence, 2500)
        self.assertTrue(Induction.objects.get(member=self.who, category="lathe").paid)

    def test_payments_cant_be_added_or_changed_in_the_ordinary_way(self):
        self.assertEqual(self.client.get("/admin/payments/payment/add/").status_code, 403)
        payment = services.record_payment(self.who, Payment.Reason.SUBSCRIPTION, Payment.Source.GOCARDLESS, 2000)
        page = self.client.get(f"/admin/payments/payment/{payment.pk}/change/")
        self.assertNotContains(page, 'name="amount_pence"')

    def test_only_cash_credit_rows_can_be_deleted(self):
        cash = services.record_payment(self.who, Payment.Reason.BALANCE, Payment.Source.CASH, 500)
        card = services.record_payment(self.who, Payment.Reason.BALANCE, Payment.Source.STRIPE, 500)
        self.assertEqual(self.client.get(f"/admin/payments/payment/{cash.pk}/delete/").status_code, 200)
        self.assertEqual(self.client.get(f"/admin/payments/payment/{card.pk}/delete/").status_code, 403)

    def test_list_shows_the_total(self):
        services.record_payment(self.who, Payment.Reason.BALANCE, Payment.Source.CASH, 500)
        services.record_payment(self.who, Payment.Reason.SUBSCRIPTION, Payment.Source.STANDING_ORDER, 2000)
        self.assertContains(self.client.get("/admin/payments/payment/"), "£25.00")


class CreditPageTests(TestCase):
    def setUp(self):
        self.who = member()
        self.client.force_login(self.who)
        services.record_payment(self.who, Payment.Reason.BALANCE, Payment.Source.CASH, 2000)
        services.pay_from_credit(self.who, Payment.Reason.CONSUMABLE, 350, reference="printer")

    def test_shows_balance_and_history(self):
        page = self.client.get(reverse("payments:credit"))
        self.assertContains(page, "£16.50")
        self.assertContains(page, "−£3.50")
        self.assertContains(page, "Credit top-up · Cash")

    def test_needs_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("payments:credit")).status_code, 302)

    def test_other_members_movements_are_not_shown(self):
        other = member()
        services.record_payment(other, Payment.Reason.BALANCE, Payment.Source.CASH, 9900)
        self.assertNotContains(self.client.get(reverse("payments:credit")), "£99.00")

    def ask(self, amount="10", sort_code="12 34 56", account="12345678"):
        return self.client.post(
            reverse("payments:credit"),
            {"amount": amount, "account_name": "M Member", "sort_code": sort_code, "account_number": account},
            follow=True,
        )

    def test_asking_for_a_withdrawal(self):
        self.assertContains(self.ask(), "the trustees will pay it")
        request = WithdrawalRequest.objects.get()
        self.assertEqual((request.amount_pence, request.sort_code), (1000, "12-34-56"))
        self.assertEqual(credit_pence(self.who), 1650)  # nothing moves until it's paid
        self.assertContains(self.ask(amount="1"), "already have a withdrawal waiting")

    def test_withdrawal_checks(self):
        self.assertContains(self.ask(amount="16.51"), "more than your credit")
        self.assertContains(self.ask(sort_code="1234"), "6 digits")
        self.assertContains(self.ask(account="123"), "8 digits")
        self.assertFalse(WithdrawalRequest.objects.exists())

    def test_paying_it_takes_the_credit_and_forgets_the_bank_details(self):
        self.ask()
        admin = Member.objects.create_superuser("admin@example.com", "a-good-password", given_name="A", family_name="D")
        self.client.force_login(admin)
        request = WithdrawalRequest.objects.get()
        self.client.post(
            "/admin/payments/withdrawalrequest/",
            {"action": "mark_paid", "_selected_action": [request.pk]},
        )
        request.refresh_from_db()
        self.assertEqual(request.status, WithdrawalRequest.Status.PAID)
        self.assertEqual((request.account_name, request.sort_code, request.account_number), ("", "", ""))
        self.assertEqual(request.payment.reason, Payment.Reason.WITHDRAWAL)
        self.assertEqual(credit_pence(self.who), 650)
