"""The nightly jobs: creating charges, billing, checking memberships (spec 03 §3.3-3.6)."""

from datetime import date, timedelta
from io import StringIO

from django.contrib.auth.models import Group
from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from apps.billing import billing, membership
from apps.billing.models import Charge
from apps.members.models import Member
from apps.payments.credit import credit_pence
from apps.payments.models import Payment
from apps.site_settings.models import SiteSettings

from .helpers import TODAY, BillingTestCase, payer, top_up


class DateTests(BillingTestCase):
    def test_add_month(self):
        self.assertEqual(membership.add_month(date(2026, 1, 31)), date(2026, 2, 28))
        self.assertEqual(membership.add_month(date(2028, 1, 31)), date(2028, 2, 29))
        self.assertEqual(membership.add_month(date(2026, 12, 15)), date(2027, 1, 15))

    def test_new_payment_day_is_today_or_the_1st(self):
        self.assertEqual(membership.payment_day_for(date(2026, 3, 28)), 28)
        self.assertEqual(membership.payment_day_for(date(2026, 3, 29)), 1)


class CreateChargesTests(BillingTestCase):
    def test_a_week_ahead_for_active_members_and_safe_to_run_twice(self):
        soon = payer(day=(TODAY + timedelta(days=3)).day)
        later = payer(day=(TODAY + timedelta(days=8)).day)
        payer(day=TODAY.day, status=Member.Status.HONORARY)
        payer(day=TODAY.day, status=Member.Status.SUSPENDED)
        payer(day=TODAY.day, status=Member.Status.LEAVING)
        self.assertEqual(billing.create_charges(TODAY), {"subscription": 1, "locker-rent": 0})
        self.assertEqual(billing.create_charges(TODAY), {"subscription": 0, "locker-rent": 0})
        charge = Charge.objects.get()
        self.assertEqual(
            (charge.member, charge.charge_date, charge.status), (soon, TODAY + timedelta(days=3), "pending")
        )
        self.assertFalse(later.charges.exists())

    def test_locker_rent_for_active_and_honorary_members(self):
        active = payer()
        honorary = payer(status=Member.Status.HONORARY)
        self.rent_locker(active)
        self.rent_locker(honorary)
        self.rent_locker(payer(), override=0)  # free: no charge
        billing.create_charges(TODAY)
        rent = Charge.objects.filter(kind=Charge.Kind.LOCKER_RENT)
        self.assertEqual({c.member for c in rent}, {active, honorary})
        self.assertFalse(honorary.charges.filter(kind=Charge.Kind.SUBSCRIPTION).exists())


class BillCreditPayersTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.member = payer(Member.PaymentMethod.CREDIT)
        billing.create_charges(TODAY)

    def test_paid_from_credit(self):
        top_up(self.member, 3000)
        self.assertEqual(billing.bill_members(TODAY), {"paid from credit": 1})
        charge = Charge.objects.get()
        self.assertEqual((charge.status, charge.amount_pence, charge.paid_on), ("paid", 2500, TODAY))
        self.assertEqual(credit_pence(self.member), 500)
        self.member.refresh_from_db()
        self.assertEqual(self.member.subscription_expires, date(2027, 1, 10))
        self.assertEqual(charge.payments.get().source, Payment.Source.BALANCE)
        self.assertEqual(billing.bill_members(TODAY), {})  # nothing left to bill

    def test_not_enough_credit_emails_once_and_tries_again_each_night(self):
        top_up(self.member, 1000)
        billing.bill_members(TODAY)
        billing.bill_members(TODAY + timedelta(days=1))
        self.assertEqual(Charge.objects.get().status, "due")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("couldn't be paid", mail.outbox[0].subject)
        top_up(self.member, 2000)
        billing.bill_members(TODAY + timedelta(days=2))
        self.assertEqual(Charge.objects.get().status, "paid")

    def test_direct_debit_is_the_backup(self):
        self.member.mandate_id = "MD-BACKUP"
        self.member.save()
        billing.bill_members(TODAY)
        charge = Charge.objects.get()
        self.assertEqual(charge.status, "processing")
        (payment,) = self.gc.payments.values()
        self.assertEqual((payment["mandate"], payment["amount"]), ("MD-BACKUP", 2500))
        self.assertIn("Direct Debit instead", mail.outbox[0].subject)

    def test_the_amount_is_taken_when_billed(self):
        self.member.monthly_subscription_pence = 3000
        self.member.save()
        top_up(self.member, 3000)
        billing.bill_members(TODAY)
        self.assertEqual(Charge.objects.get().amount_pence, 3000)


class BillDirectDebitTests(BillingTestCase):
    def test_one_payment_for_subscription_and_rent(self):
        member = payer()
        rental = self.rent_locker(member)
        billing.create_charges(TODAY)
        self.assertEqual(billing.bill_members(TODAY), {"direct debit": 1})
        ((payment_id, payment),) = self.gc.payments.items()
        self.assertEqual(payment["amount"], 3300)
        self.assertEqual(payment["description"], "Build Brighton subscription and locker rent")
        rows = Payment.objects.filter(source_id=payment_id)
        self.assertEqual(sorted(r.reason for r in rows), ["locker-rent", "subscription"])
        self.assertTrue(all(r.status == "pending" for r in rows))
        self.assertEqual(set(Charge.objects.values_list("status", flat=True)), {"processing"})
        self.assertEqual(Charge.objects.get(rental=rental).amount_pence, 800)
        member.refresh_from_db()
        self.assertEqual(member.subscription_expires, date(2027, 1, 10))  # charge date + 1 month
        billing.bill_members(TODAY)
        self.assertEqual(len(self.gc.payments), 1)  # never billed twice

    def test_a_crash_after_gocardless_took_the_payment_is_safe_to_rerun(self):
        member = payer()
        billing.create_charges(TODAY)
        charge = Charge.objects.get()
        charge.status, charge.amount_pence = "due", 2500
        charge.save()
        key = f"bbms-charges-{charge.pk}"
        self.gc.create_payment(member.mandate_id, 2500, "x", idempotency_key=key, metadata={})
        billing.bill_members(TODAY)
        self.assertEqual(len(self.gc.payments), 1)
        self.assertEqual(charge.payments.get().source_id, next(iter(self.gc.payments)))

    def test_gocardless_errors_leave_the_charge_due(self):
        payer()
        billing.create_charges(TODAY)
        self.gc.fail_next = "GoCardless is down"
        self.assertEqual(billing.bill_members(TODAY), {"error": 1})
        self.assertEqual(Charge.objects.get().status, "due")
        self.assertFalse(Payment.objects.exists())

    def test_standing_orders_are_left_for_the_statement_import(self):
        payer(Member.PaymentMethod.STANDING_ORDER)
        billing.create_charges(TODAY)
        self.assertEqual(billing.bill_members(TODAY), {"paid outside BBMS": 1})
        self.assertEqual(Charge.objects.get().status, "due")


class ActivateTests(BillingTestCase):
    def test_direct_debit_payer_is_billed_straight_away(self):
        new = payer(status=Member.Status.SETTING_UP)
        membership.activate(new, TODAY)
        new.refresh_from_db()
        self.assertEqual(new.status, "active")
        self.assertEqual(Charge.objects.get().status, "processing")
        self.assertEqual(len(self.gc.payments), 1)
        self.assertEqual(mail.outbox[0].subject, "Welcome to Build Brighton")

    def test_credit_payer_is_billed_at_night(self):
        new = payer(Member.PaymentMethod.CREDIT, status=Member.Status.SETTING_UP)
        membership.activate(new, TODAY)
        self.assertEqual(Charge.objects.get().status, "due")

    def test_after_the_28th_with_payment_day_1_the_charge_is_next_month(self):
        new = payer(Member.PaymentMethod.CREDIT, day=1, status=Member.Status.SETTING_UP)
        membership.activate(new, date(2026, 12, 30))
        self.assertEqual(Charge.objects.get().charge_date, date(2027, 1, 1))

    def test_nothing_new_while_paid_up(self):
        back = payer(status=Member.Status.LEAVING, subscription_expires=TODAY + timedelta(days=5))
        membership.activate(back, TODAY)
        back.refresh_from_db()
        self.assertEqual(back.status, "active")
        self.assertFalse(Charge.objects.exists())
        self.assertEqual(mail.outbox, [])  # welcome only when first joining


class CheckMembershipsTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.make_aware(timezone.datetime(2026, 12, 10, 6, 0))

    def check(self, today=TODAY):
        return membership.check_memberships(today, self.now + (today - TODAY))

    def test_active_member_is_suspended_after_the_grace_period(self):
        late = payer(subscription_expires=TODAY - timedelta(days=11))
        within = payer(subscription_expires=TODAY - timedelta(days=10))
        standing_order = payer(Member.PaymentMethod.STANDING_ORDER, subscription_expires=TODAY - timedelta(days=100))
        honorary = payer(status=Member.Status.HONORARY)
        self.assertEqual(self.check()["suspended"], 1)
        for who, status in (
            (late, "suspended"),
            (within, "active"),
            (standing_order, "active"),
            (honorary, "honorary"),
        ):
            who.refresh_from_db()
            self.assertEqual(who.status, status, who)
        self.assertIn("suspended", mail.outbox[0].subject)

    def test_a_paid_charge_extends_rather_than_suspends(self):
        member = payer(subscription_expires=TODAY - timedelta(days=40))
        Charge.objects.create(member=member, charge_date=TODAY - timedelta(days=5), status="paid", paid_on=TODAY)
        self.assertEqual(self.check()["extended"], 1)
        member.refresh_from_db()
        self.assertEqual((member.status, member.subscription_expires), ("active", date(2027, 1, 5)))

    def test_suspended_member_is_reactivated_by_a_payment_or_leaves_after_30_days(self):
        paid = payer(status=Member.Status.SUSPENDED, suspended_at=self.now - timedelta(days=3))
        Charge.objects.create(member=paid, charge_date=TODAY, status="paid", paid_on=TODAY)
        unpaid = payer(status=Member.Status.SUSPENDED, suspended_at=self.now - timedelta(days=29))
        self.check()
        paid.refresh_from_db()
        unpaid.refresh_from_db()
        self.assertEqual((paid.status, paid.subscription_expires), ("active", date(2027, 1, 10)))
        self.assertEqual(unpaid.status, "suspended")
        self.check(TODAY + timedelta(days=1))
        unpaid.refresh_from_db()
        self.assertEqual((unpaid.status, unpaid.payment_day), ("left", None))

    def test_suspended_without_a_date_starts_the_30_days_now(self):
        imported = payer(status=Member.Status.SUSPENDED)
        self.check()
        imported.refresh_from_db()
        self.assertEqual((imported.status, imported.suspended_at), ("suspended", self.now))

    def test_leaving_member_leaves_after_their_paid_time(self):
        staying = payer(status=Member.Status.LEAVING, subscription_expires=TODAY)
        going = payer(status=Member.Status.LEAVING, subscription_expires=TODAY - timedelta(days=1))
        self.check()
        staying.refresh_from_db()
        going.refresh_from_db()
        self.assertEqual((staying.status, going.status), ("leaving", "left"))

    def test_leaving_releases_lockers_cancels_charges_and_tells_the_storage_team(self):
        team = Group.objects.create(name="storage")
        keeper = payer(subscription_expires=TODAY + timedelta(days=20))
        keeper.groups.add(team)
        going = payer(status=Member.Status.LEAVING, subscription_expires=TODAY - timedelta(days=1))
        rental = self.rent_locker(going)
        Charge.objects.create(member=going, charge_date=TODAY + timedelta(days=2))
        self.check()
        rental.refresh_from_db()
        self.assertIsNotNone(rental.ended_at)
        self.assertEqual(Charge.objects.get(member=going).status, "cancelled")
        subjects = sorted(m.subject for m in mail.outbox)
        self.assertEqual(subjects, ["Build Brighton: lockers to clear", "Build Brighton: your membership has ended"])
        to_team = next(m for m in mail.outbox if "clear" in m.subject)
        self.assertEqual(to_team.to, [keeper.email])

    def test_settings_change_the_rules(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"grace_direct_debit_days": 20})
        member = payer(subscription_expires=TODAY - timedelta(days=15))
        self.check()
        member.refresh_from_db()
        self.assertEqual(member.status, "active")


class CommandTests(BillingTestCase):
    def test_dry_run_changes_nothing_and_sends_nothing(self):
        payer()
        payer(Member.PaymentMethod.CREDIT, subscription_expires=TODAY - timedelta(days=30))
        out = StringIO()
        call_command("create_charges", "--date", TODAY.isoformat(), "--dry-run", stdout=out)
        self.assertIn("Dry run", out.getvalue())
        self.assertIn("subscription 2", out.getvalue())
        self.assertFalse(Charge.objects.exists())
        call_command("create_charges", "--date", TODAY.isoformat(), stdout=StringIO())
        out = StringIO()
        call_command("bill_members", "--date", TODAY.isoformat(), "--dry-run", stdout=out)
        self.assertIn("Would have sent GoCardless 1 request(s)", out.getvalue())
        self.assertEqual(self.gc.payments, {})
        self.assertFalse(Charge.objects.exclude(status="pending").exists())
        call_command("check_memberships", "--date", TODAY.isoformat(), "--dry-run", stdout=StringIO())
        self.assertEqual(mail.outbox, [])
        self.assertFalse(Member.objects.filter(status="suspended").exists())

    def test_running_the_night_twice_makes_nothing_extra(self):
        payer()
        credit = payer(Member.PaymentMethod.CREDIT)
        top_up(credit, 10_000)
        for _ in range(2):
            for job in ("create_charges", "bill_members", "check_memberships"):
                call_command(job, "--date", TODAY.isoformat(), stdout=StringIO())
        self.assertEqual(Charge.objects.count(), 2)
        self.assertEqual(len(self.gc.payments), 1)
        self.assertEqual(Payment.objects.filter(reason="subscription").count(), 2)
