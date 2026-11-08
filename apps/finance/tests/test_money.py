"""Income and spending by heading, and what each machine has made (04 §4.9; trustees, 2026-11-08)."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.equipment.models import Equipment, UsageSession
from apps.equipment.tests.helpers import laser, member
from apps.expenses import claims
from apps.expenses.models import Expense
from apps.finance import money
from apps.finance.tests.test_statements import finance_member
from apps.members.models import Member
from apps.payments import services
from apps.payments.models import Payment

R, S = Payment.Reason, Payment.Source
TODAY = timezone.localdate()


def approved_expense(who, amount, equipment=None, category="tools"):
    expense = Expense.objects.create(
        member=who, category=category, description="Made up", amount_pence=amount, spent_on=TODAY, equipment=equipment
    )
    approver = Member.objects.filter(is_superuser=True).first() or Member.objects.create_superuser(
        "t@example.com", "a-good-password", given_name="T", family_name="T"
    )
    return claims.approve(expense.pk, approver)


def session_fee(who, machine, pence):
    session = UsageSession.objects.create(
        equipment=machine, member=who, started_at=timezone.now() - timedelta(hours=1), ended_at=timezone.now()
    )
    return Payment.objects.create(
        member=who, reason=R.EQUIPMENT_FEE, source=S.BALANCE, amount_pence=pence, usage_session=session
    )


class SummaryTests(TestCase):
    def test_headings(self):
        kim = member()
        services.record_payment(kim, R.SUBSCRIPTION, S.STRIPE, 2000, fee_pence=68)
        services.record_payment(kim, R.SUBSCRIPTION, S.GOCARDLESS, 1500, status=Payment.Status.PENDING)
        services.record_payment(kim, R.KEY_DEPOSIT, S.CASH, 1500)
        services.record_payment(kim, R.BALANCE, S.CASH, 1000)  # held for Kim, not income
        services.pay_from_credit(kim, R.CONSUMABLE, 300, reference="printer")  # income when spent
        services.record_payment(kim, R.SUBSCRIPTION, S.CASH, 900, created_at=timezone.now() - timedelta(days=400))
        approved_expense(kim, 450, category="consumables")  # paid into credit: spending, not a top-up
        summary = money.summary(TODAY.replace(day=1), TODAY)
        headings = {h["key"]: (h["gross"], h["fees"], h["net"]) for h in summary["headings"]}
        self.assertEqual(headings, {"monthly": (2000, 68, 1932), "oneoff": (1500, 0, 1500), "use": (300, 0, 300)})
        self.assertEqual((summary["gross"], summary["net"], summary["pending"]), (3800, 3732, 1500))
        self.assertEqual((summary["credit_added"], summary["spent"]), (1000, 450))
        self.assertEqual(summary["after_expenses"], 3732 - 450)
        self.assertEqual(summary["expenses"][0]["label"], "Consumables")

    def test_a_refund_lowers_its_heading(self):
        kim = member()
        paid = services.pay_induction(kim, laser(induction_fee_pence=2500), S.STRIPE)
        services.refund_to_credit(paid, kim)
        summary = money.summary(TODAY.replace(day=1), TODAY)
        self.assertEqual(summary["headings"][1]["gross"], 0)
        self.assertEqual(summary["credit_added"], 2500)  # the money is now held for Kim

    def test_twelve_months(self):
        kim = member()
        services.record_payment(kim, R.SUBSCRIPTION, S.CASH, 2000)
        services.record_payment(kim, R.LOCKER_RENT, S.CASH, 500, created_at=timezone.now() - timedelta(days=40))
        rows = money.months(TODAY)
        self.assertEqual(len(rows), 12)
        self.assertEqual(
            (rows[-1]["month"], rows[-1]["monthly"], rows[-1]["income"]), (TODAY.replace(day=1), 2000, 2000)
        )
        self.assertEqual(sum(r["use"] for r in rows), 500)


class MachineTests(TestCase):
    def test_induction_plus_use_less_expenses(self):
        kim = member()
        red = laser(induction_fee_pence=2500)
        blue = laser(name="Blue laser", slug="blue-laser", induction_fee_pence=2500)  # same "laser" induction
        lathe = Equipment.objects.create(name="Lathe", slug="lathe")
        services.pay_induction(kim, red, S.CASH)
        session_fee(kim, red, 600)
        session_fee(kim, blue, 400)
        session_fee(kim, lathe, 100)
        approved_expense(kim, 1500, equipment=blue)
        approved_expense(kim, 200)  # not for a machine
        report = money.machines()
        rows = [
            ([m.name for m in r["machines"]], r["induction"], r["usage"], r["expenses"], r["net"])
            for r in report["rows"]
        ]
        self.assertEqual(rows, [(["Blue laser", "Red laser"], 2500, 1000, 1500, 2000), (["Lathe"], 0, 100, 0, 100)])
        self.assertEqual((report["net"], report["unlinked_expenses"]), (2100, 200))

    def test_page(self):
        kim = member()
        red = laser(induction_fee_pence=2500)
        services.pay_induction(kim, red, S.CASH)
        self.client.force_login(finance_member())
        page = self.client.get("/finance/machines/")
        self.assertContains(page, "All time")
        self.assertContains(page, "Red laser")
        self.assertContains(page, "£25.00")
        page = self.client.get(f"/finance/machines/?year={TODAY.year - 1}")
        self.assertEqual(page.context["report"]["induction"], 0)
        income = self.client.get("/finance/income/")
        self.assertContains(income, f"Income and spending: {TODAY:%B %Y}")
        self.assertContains(income, "One-off payments")
        self.client.force_login(member())
        self.assertEqual(self.client.get("/finance/machines/").status_code, 403)


class ExpenseMachineTests(TestCase):
    def test_claim_for_a_machine_and_correct_it_later(self):
        import tempfile

        from django.test import override_settings

        from apps.expenses.tests.test_expenses import pdf

        kim, red = member(), laser()
        Equipment.objects.create(name="Old saw", slug="old-saw", archived=True)
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="bbms-test-media-")):
            self.client.force_login(kim)
            form = self.client.get("/expenses/claim/")
            self.assertContains(form, "Red laser")
            self.assertNotContains(form, "Old saw")
            self.client.post(
                "/expenses/claim/",
                {
                    "category": "equipment-repair",
                    "description": "New lens",
                    "amount": "30",
                    "spent_on": TODAY,
                    "equipment": red.pk,
                    "receipt": pdf(),
                },
            )
            expense = Expense.objects.get()
            self.assertEqual(expense.equipment, red)
            # The member can't change it afterwards; finance can.
            self.assertEqual(self.client.post(f"/expenses/{expense.pk}/machine/", {"equipment": ""}).status_code, 404)
            self.client.force_login(finance_member())
            self.client.post(f"/expenses/{expense.pk}/machine/", {"equipment": ""})
            expense.refresh_from_db()
            self.assertIsNone(expense.equipment)
            page = self.client.get(f"/expenses/?machine={red.pk}")
            self.assertEqual(page.context["page"].paginator.count, 0)
