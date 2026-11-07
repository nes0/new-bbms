"""Member credit and spending it (spec 04 §4.1, §4.7)."""

from django.test import TestCase

from apps.equipment.models import Equipment, Induction
from apps.equipment.tests.helpers import member
from apps.payments import services
from apps.payments.credit import credit_by_member, credit_movements, credit_pence
from apps.payments.models import Payment

R, S, ST = Payment.Reason, Payment.Source, Payment.Status


def add(who, reason, source, pence, status=ST.PAID):
    return Payment.objects.create(member=who, reason=reason, source=source, amount_pence=pence, status=status)


class CreditTests(TestCase):
    def test_top_ups_minus_spending(self):
        who = member()
        add(who, R.BALANCE, S.STRIPE, 2000)
        add(who, R.EQUIPMENT_FEE, S.BALANCE, 450)
        add(who, R.SUBSCRIPTION, S.GOCARDLESS, 2500)  # neither in nor out of credit
        self.assertEqual(credit_pence(who), 1550)

    def test_the_legacy_statuses_count(self):
        who = member()
        add(who, R.BALANCE, S.GOCARDLESS, 1000, ST.PENDING)
        add(who, R.BALANCE, S.CASH, 300, ST.WITHDRAWN)
        add(who, R.BALANCE, S.GOCARDLESS, 5000, ST.FAILED)
        add(who, R.BALANCE, S.GOCARDLESS, 5000, ST.CANCELLED)
        self.assertEqual(credit_pence(who), 1300)

    def test_a_negative_correction_gives_credit_back(self):
        who = member()
        add(who, R.EQUIPMENT_FEE, S.BALANCE, 300)
        add(who, R.EQUIPMENT_FEE, S.BALANCE, -200)
        self.assertEqual(credit_pence(who), -100)

    def test_each_members_credit_is_their_own(self):
        a, b = member(), member()
        add(a, R.BALANCE, S.CASH, 500)
        add(b, R.EQUIPMENT_FEE, S.BALANCE, 75)
        self.assertEqual((credit_pence(a), credit_pence(b)), (500, -75))
        self.assertEqual(credit_by_member(), {a.pk: 500, b.pk: -75})

    def test_movements_and_their_effect(self):
        who = member()
        top_up = add(who, R.BALANCE, S.CASH, 500)
        fee = add(who, R.EQUIPMENT_FEE, S.BALANCE, 75)
        add(who, R.SUBSCRIPTION, S.GOCARDLESS, 2500)
        self.assertEqual(set(credit_movements(who)), {top_up, fee})
        self.assertEqual((top_up.credit_effect_pence, fee.credit_effect_pence), (500, -75))


class SpendingCreditTests(TestCase):
    def setUp(self):
        self.who = member()
        add(self.who, R.BALANCE, S.CASH, 1000)

    def test_paying_from_credit(self):
        payment = services.pay_from_credit(self.who, R.CONSUMABLE, 400, reference="printer")
        self.assertEqual((payment.source, payment.status), (S.BALANCE, ST.PAID))
        self.assertEqual(credit_pence(self.who), 600)

    def test_credit_cant_go_below_zero(self):
        with self.assertRaises(services.InsufficientCredit):
            services.pay_from_credit(self.who, R.CONSUMABLE, 1001)
        services.pay_from_credit(self.who, R.CONSUMABLE, 1000)
        self.assertEqual(credit_pence(self.who), 0)

    def test_amounts_must_be_positive(self):
        for pence in (0, -5):
            with self.assertRaises(ValueError):
                services.pay_from_credit(self.who, R.CONSUMABLE, pence)
            with self.assertRaises(ValueError):
                services.record_payment(self.who, R.BALANCE, S.CASH, pence)

    def test_induction_fee_from_credit_marks_the_induction_paid(self):
        lathe = Equipment.objects.create(name="Lathe", slug="lathe", requires_induction=True, induction_fee_pence=800)
        payment = services.pay_induction(self.who, lathe, S.BALANCE)
        induction = Induction.objects.get(member=self.who, category="lathe")
        self.assertTrue(induction.paid)
        self.assertEqual((induction.payment, payment.amount_pence, payment.reference), (payment, 800, "lathe"))
        self.assertEqual(credit_pence(self.who), 200)

    def test_induction_fee_needs_enough_credit(self):
        lathe = Equipment.objects.create(name="Lathe", slug="lathe", induction_fee_pence=1500)
        with self.assertRaises(services.InsufficientCredit):
            services.pay_induction(self.who, lathe, S.BALANCE)
        self.assertFalse(Induction.objects.exists())

    def test_fixed_prices(self):
        self.assertEqual(services.pay_key_deposit(self.who, S.CASH).amount_pence, 1500)
        self.assertEqual(services.pay_box_fee(self.who, S.MANUAL).amount_pence, 1500)
        self.assertEqual(credit_pence(self.who), 1000)  # paid in cash and outside BBMS: credit untouched
