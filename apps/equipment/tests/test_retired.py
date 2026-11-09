"""Retiring equipment and reviewing its lifetime figures (spec 05 §5.7, "Retired equipment")."""

from datetime import date, timedelta

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.equipment import lifetime, rules
from apps.equipment.models import Equipment, Induction
from apps.expenses import claims
from apps.expenses.models import Expense
from apps.members.models import Member
from apps.payments import services
from apps.payments.models import Payment

from .helpers import laser, member, trained


def admin():
    return Member.objects.create_superuser("boss@example.com", "a-good-password", given_name="Bo", family_name="Ss")


def used(machine, who, minutes):
    services.record_payment(who, Payment.Reason.BALANCE, Payment.Source.CASH, 5000)
    session = rules.start_session(machine, who, close_existing=False)
    session.started_at = timezone.now() - timedelta(minutes=minutes)
    session.save(update_fields=["started_at"])
    return rules.end_session(session, ended_by="tap")


class LifetimeTests(TestCase):
    def setUp(self):
        self.boss = admin()
        self.machine = laser(
            induction_fee_pence=5000,
            purchase_price_pence=40000,
            obtained_at=date(2020, 1, 1),
            removed_at=date(2025, 1, 1),
            archived=True,
        )
        self.kim = member()
        services.pay_induction(self.kim, self.machine, Payment.Source.CASH)
        Induction.objects.filter(member=self.kim).update(trained_at=timezone.now())
        used(self.machine, self.kim, 120)  # £3.00 an hour: £6.00
        expense = Expense.objects.create(
            member=self.kim,
            category="equipment",
            description="New lens",
            amount_pence=2500,
            spent_on=date(2024, 5, 1),
            equipment=self.machine,
        )
        claims.approve(expense.pk, self.boss)

    def test_what_it_brought_in_against_what_it_cost(self):
        life = lifetime.lifetime(self.machine)
        self.assertEqual((life.access_fees, life.usage_fees, life.expenses), (5000, 600, 2500))
        self.assertEqual(life.net, 5000 + 600 - 2500 - 40000)
        self.assertEqual((life.sessions, life.users, life.trained), (1, 1, 1))
        self.assertAlmostEqual(life.years, 5.0, places=1)

    def test_admins_see_the_page_and_the_retired_machines_page(self):
        self.client.force_login(self.boss)
        response = self.client.get(reverse("equipment:retired"))
        self.assertContains(response, "Red laser")
        self.assertContains(response, "£400.00")  # bought for
        self.assertContains(response, "−£369.00")  # net: £50 + £6 − £25 − £400
        page = self.client.get(reverse("equipment:detail", args=["laser"]))
        self.assertContains(page, "Retired on 1 January 2025")
        self.assertNotContains(page, "Report a problem")
        self.assertNotContains(page, "Record a service")

    def test_finance_readers_see_it_but_members_dont(self):
        self.client.force_login(self.kim)
        self.assertEqual(self.client.get(reverse("equipment:retired")).status_code, 404)
        self.assertEqual(self.client.get(reverse("equipment:detail", args=["laser"])).status_code, 404)
        self.kim.user_permissions.add(Permission.objects.get(codename="see_finance"))
        self.assertEqual(self.client.get(reverse("equipment:retired")).status_code, 200)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("equipment:retired")).status_code, 302)

    def test_a_shared_access_fee_is_noted(self):
        Equipment.objects.create(name="Blue laser", slug="blue", requires_induction=True, induction_category="laser")
        self.client.force_login(self.boss)
        self.assertContains(self.client.get(reverse("equipment:retired")), "Access fee shared with")


class RetiringInTheAdminTests(TestCase):
    def setUp(self):
        self.boss = admin()
        self.client.force_login(self.boss)
        self.machine = laser()

    def test_retiring_keeps_it_with_the_date(self):
        url = reverse("admin:equipment_equipment_changelist")
        self.client.post(url, {"action": "retire", "_selected_action": [self.machine.pk]})
        self.machine.refresh_from_db()
        self.assertTrue(self.machine.archived)
        self.assertEqual(self.machine.removed_at, timezone.localdate())
        self.assertContains(self.client.get(url), reverse("equipment:retired"))

    def test_equipment_with_history_cant_be_deleted_only_retired(self):
        delete = reverse("admin:equipment_equipment_delete", args=[self.machine.pk])
        self.assertEqual(self.client.get(delete).status_code, 200)  # nothing recorded against it yet
        trained(member(), self.machine)
        self.assertEqual(self.client.get(delete).status_code, 403)
        actions = self.client.get(reverse("admin:equipment_equipment_changelist")).context["action_form"]
        self.assertNotIn("delete_selected", [value for value, _ in actions.fields["action"].choices])


class AccessFeeWordingTests(TestCase):
    def test_members_pay_an_access_fee(self):
        machine = laser(induction_fee_pence=2500)
        kim = member()
        self.client.force_login(kim)
        self.assertContains(self.client.get(reverse("payments:pay")), "Access fee: Red laser")
        self.assertEqual(Payment.Reason.INDUCTION.label, "Equipment access fee")
        self.assertContains(self.client.get(reverse("equipment:detail", args=[machine.slug])), "one-off access fee")
