"""Members renting and giving up lockers themselves, and notice of a higher default rent (spec 06 §6.2)."""

from datetime import date, timedelta
from unittest import mock

from django.contrib.auth.models import Group
from django.core import mail

from apps.billing import billing, gocardless
from apps.billing.models import Charge
from apps.billing.tests.helpers import TODAY, BillingTestCase, payer, top_up
from apps.equipment.tests.helpers import member
from apps.members.models import Member
from apps.notifications.models import Notification
from apps.payments.models import Payment

from . import rules
from .models import Locker, LockerRent, LockerRental, LockerSize, StorageLocation

DD = Member.PaymentMethod.DIRECT_DEBIT
CREDIT = Member.PaymentMethod.CREDIT


class RentingTestCase(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.location = StorageLocation.objects.create(name="Workshop wall")
        self.size = LockerSize.objects.create(name="Large")
        self.rent = LockerRent.objects.create(location=self.location, size=self.size, monthly_rent_pence=800)
        self.locker = Locker.objects.create(location=self.location, number=1, size=self.size)
        self.keeper = member(given_name="Stor")
        Group.objects.create(name="storage").user_set.add(self.keeper)
        # A week before the payment day (the 10th): 7 of the 30 days from 10 November to 10 December.
        self.today = mock.patch("django.utils.timezone.localdate", return_value=TODAY - timedelta(days=7))
        self.today.start()
        self.addCleanup(self.today.stop)

    def rent_it(self, who, locker=None):
        return rules.rent_locker(locker or self.locker, who)


class FirstRentTests(RentingTestCase):
    def test_the_first_rent_covers_the_days_until_the_payment_day(self):
        who = payer(day=10)
        self.assertEqual(rules.first_rent(who, 800, date(2026, 12, 3)), (187, date(2026, 12, 10)))  # 800 × 7/30
        self.assertEqual(rules.first_rent(who, 800, date(2026, 12, 20)), (542, date(2027, 1, 10)))  # 800 × 21/31
        self.assertEqual(rules.first_rent(who, 800, date(2026, 12, 10)), (800, date(2027, 1, 10)))  # a whole month
        self.assertEqual(rules.first_rent(payer(day=1), 800, date(2027, 1, 15)), (439, date(2027, 2, 1)))

    def test_a_direct_debit_payer_rents_and_pays_the_first_rent_at_once(self):
        who = payer(DD)
        rental, amount = self.rent_it(who)
        self.assertEqual(amount, 187)
        self.assertEqual(rules.status(self.locker), rules.RENTED)
        charge = Charge.objects.get(rental=rental)
        self.assertEqual((charge.first_rent, charge.amount_pence, charge.status), (True, 187, "processing"))
        (sent,) = self.gc.payments.values()
        self.assertEqual((sent["amount"], sent["mandate"]), (187, "MD-TEST1"))
        self.assertEqual(Payment.objects.get(charge=charge).status, Payment.Status.PENDING)
        self.assertEqual(rental.started_by, who)
        subjects = sorted(m.subject for m in mail.outbox)
        self.assertEqual(len(subjects), 2)
        self.assertIn("you're renting Workshop wall locker 1", " ".join(subjects))
        self.assertTrue(Notification.objects.filter(member=self.keeper, kind="locker rented").exists())

    def test_a_credit_payer_pays_from_credit(self):
        who = payer(CREDIT)
        top_up(who, 500)
        rental, _ = self.rent_it(who)
        charge = Charge.objects.get(rental=rental)
        self.assertEqual(charge.status, "paid")
        self.assertEqual(Payment.objects.get(charge=charge).source, Payment.Source.BALANCE)
        self.assertEqual(self.gc.payments, {})

    def test_too_little_credit_and_no_direct_debit_rents_nothing(self):
        who = payer(CREDIT)
        top_up(who, 100)
        with self.assertRaisesMessage(rules.LockerError, "The first rent is £1.87 and you have £1.00 of credit"):
            self.rent_it(who)
        self.assertFalse(LockerRental.objects.exists())
        self.assertFalse(Charge.objects.filter(kind="locker-rent").exists())
        self.assertEqual(mail.outbox, [])

    def test_too_little_credit_uses_the_direct_debit_backup(self):
        who = payer(CREDIT, mandate_id="MD-BACKUP")
        rental, _ = self.rent_it(who)
        self.assertEqual(Charge.objects.get(rental=rental).status, "processing")
        (sent,) = self.gc.payments.values()
        self.assertEqual(sent["mandate"], "MD-BACKUP")

    def test_only_direct_debit_or_credit_payers_can_rent(self):
        for method in (Member.PaymentMethod.PAYPAL, Member.PaymentMethod.STANDING_ORDER, ""):
            with self.subTest(method=method), self.assertRaisesMessage(rules.LockerError, "Direct Debit or"):
                self.rent_it(payer(method))
        with self.assertRaisesMessage(rules.LockerError, "isn't set up"):
            self.rent_it(payer(DD, mandate_id=""))
        self.assertFalse(LockerRental.objects.exists())

    def test_the_limit_status_and_availability_still_apply(self):
        who = payer(DD)
        self.rent_it(who)
        other = Locker.objects.create(location=self.location, number=2, size=self.size)
        with self.assertRaisesMessage(rules.LockerError, "your limit"):
            self.rent_it(who, other)
        with self.assertRaisesMessage(rules.LockerError, "isn't available"):
            self.rent_it(payer(DD))
        with self.assertRaisesMessage(rules.LockerError, "Only current members"):
            self.rent_it(payer(DD, status=Member.Status.LEAVING), other)

    def test_an_honorary_member_gets_a_payment_day(self):
        who = payer(CREDIT, status=Member.Status.HONORARY, day=None)
        top_up(who, 1000)
        _, amount = self.rent_it(who)
        who.refresh_from_db()
        self.assertEqual((who.payment_day, amount), (3, 800))  # renting on the payment day pays a whole month

    def test_under_a_pound_nothing_is_taken_until_the_payment_day(self):
        self.today.stop()
        with mock.patch("django.utils.timezone.localdate", return_value=TODAY - timedelta(days=1)):
            rental, amount = self.rent_it(payer(DD))
        self.today.start()
        self.assertEqual(amount, 0)
        self.assertFalse(Charge.objects.filter(rental=rental).exists())
        self.assertEqual(self.gc.payments, {})

    def test_if_gocardless_fails_nothing_is_rented(self):
        with mock.patch.object(gocardless.FakeGoCardless, "create_payment", side_effect=gocardless.GoCardlessError):
            with self.assertRaisesMessage(rules.LockerError, "try again"):
                self.rent_it(payer(DD))
        self.assertFalse(LockerRental.objects.exists())
        self.assertFalse(Charge.objects.exists())

    def test_then_the_full_rent_on_each_payment_day(self):
        who = payer(DD)
        rental, _ = self.rent_it(who)
        first = Charge.objects.get(rental=rental)
        self.assertEqual(billing.amount_for(first), 187)  # never recomputed as a full month
        billing.create_charges(TODAY - timedelta(days=7))
        billing.bill_members(TODAY)
        on_day = Charge.objects.get(rental=rental, charge_date=TODAY)
        self.assertEqual((on_day.first_rent, on_day.amount_pence), (False, 800))


class GivingUpTests(RentingTestCase):
    def test_a_member_gives_up_their_locker(self):
        who = payer(DD)
        rental, _ = self.rent_it(who)
        mail.outbox.clear()
        rules.give_up(rental, who)
        rental.refresh_from_db()
        self.assertEqual((rental.ended_by, rental.end_reason), (who, "Given up by the member"))
        self.assertEqual(rules.status(self.locker), rules.AVAILABLE)
        self.assertEqual([m.to for m in mail.outbox], [[self.keeper.email]])
        self.assertTrue(Notification.objects.filter(member=self.keeper, kind="locker given up").exists())

    def test_nobody_else_can_give_it_up(self):
        rental, _ = self.rent_it(payer(DD))
        with self.assertRaises(rules.LockerError):
            rules.give_up(rental, payer(DD))


class PageTests(RentingTestCase):
    def test_renting_from_the_lockers_page(self):
        who = payer(DD)
        self.client.force_login(who)
        page = self.client.get("/lockers/")
        self.assertContains(page, f"/lockers/{self.locker.pk}/rent/")
        confirm = self.client.get(f"/lockers/{self.locker.pk}/rent/")
        self.assertContains(confirm, "£1.87")
        self.assertContains(confirm, "10 December 2026")
        response = self.client.post(f"/lockers/{self.locker.pk}/rent/", follow=True)
        self.assertContains(response, "Workshop wall locker 1 is yours")
        self.assertContains(response, "Give it up")
        rental = LockerRental.objects.get(member=who)
        response = self.client.post(f"/lockers/rentals/{rental.pk}/give-up/", follow=True)
        self.assertContains(response, "given up")
        rental.refresh_from_db()
        self.assertFalse(rental.is_open)

    def test_members_who_cant_pay_rent_are_told_how(self):
        self.client.force_login(payer(Member.PaymentMethod.PAYPAL))
        page = self.client.get("/lockers/")
        self.assertContains(page, "Set up your payment")
        self.assertNotContains(page, f"/lockers/{self.locker.pk}/rent/")
        self.assertEqual(self.client.post(f"/lockers/{self.locker.pk}/rent/").status_code, 200)
        self.assertFalse(LockerRental.objects.exists())

    def test_a_credit_payer_without_enough_is_asked_to_top_up(self):
        self.client.force_login(payer(CREDIT))
        self.assertContains(self.client.get(f"/lockers/{self.locker.pk}/rent/"), "Top up your credit")

    def test_renting_needs_a_confirmed_email_address(self):
        self.client.force_login(payer(DD, email_verified_at=None))
        self.assertEqual(self.client.post(f"/lockers/{self.locker.pk}/rent/").status_code, 403)
        self.assertFalse(LockerRental.objects.exists())

    def test_someone_elses_rental_cant_be_given_up(self):
        rental, _ = self.rent_it(payer(DD))
        self.client.force_login(payer(DD))
        self.assertEqual(self.client.post(f"/lockers/rentals/{rental.pk}/give-up/").status_code, 404)


class NoticeTests(RentingTestCase):
    def change_default(self, pence):
        old = self.rent.monthly_rent_pence
        self.rent.monthly_rent_pence = pence
        self.rent.save()
        return rules.default_rent_changed(self.rent, old, None, "Prices")

    def test_a_higher_default_rent_waits_30_days_after_the_email(self):
        rental, _ = self.rent_it(payer(DD))
        mail.outbox.clear()
        self.assertEqual(self.change_default(900), {"raised": 1, "lowered": 0})
        rental.refresh_from_db()
        start = TODAY - timedelta(days=7) + timedelta(days=30)
        self.assertEqual((rental.new_rent_pence, rental.new_rent_from), (900, start))
        self.assertEqual(rules.rental_rent_pence(rental, TODAY), 800)
        self.assertEqual(rules.rental_rent_pence(rental, start - timedelta(days=1)), 800)
        self.assertEqual(rules.rental_rent_pence(rental, start), 900)
        (email,) = mail.outbox
        self.assertIn("from £8.00 to £9.00", email.body)
        self.assertIn("2 January 2027", email.body)
        self.assertEqual(rules.upcoming_rent(rental), (900, start))

    def test_a_lower_default_rent_applies_at_once(self):
        rental, _ = self.rent_it(payer(DD))
        self.change_default(900)
        self.assertEqual(self.change_default(700), {"raised": 0, "lowered": 1})
        rental.refresh_from_db()
        self.assertEqual((rules.rental_rent_pence(rental, TODAY), rental.new_rent_from), (700, None))

    def test_renters_with_an_override_are_left_alone(self):
        rental, _ = self.rent_it(payer(DD))
        rules.set_rental_rent(rental, 300, "Volunteer", None)
        self.assertEqual(self.change_default(900), {"raised": 0, "lowered": 0})
        rental.refresh_from_db()
        self.assertEqual(rules.rental_rent_pence(rental, TODAY + timedelta(days=60)), 300)
        rules.set_rental_rent(rental, None, "Back to normal", None)
        rental.refresh_from_db()
        self.assertEqual(rules.rental_rent_pence(rental), 900)  # back to today's default

    def test_billing_uses_the_rent_for_the_charge_date(self):
        rental, _ = self.rent_it(payer(DD))
        self.change_default(900)
        rental.refresh_from_db()
        later = Charge.objects.create(
            member=rental.member, kind="locker-rent", rental=rental, charge_date=date(2027, 1, 10)
        )
        soon = Charge.objects.create(member=rental.member, kind="locker-rent", rental=rental, charge_date=TODAY)
        self.assertEqual((billing.amount_for(soon), billing.amount_for(later)), (800, 900))
