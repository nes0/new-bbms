"""The "box paid" flag: boxes have no inventory, only whether a member has paid the one-off fee (spec 06 §6.2)."""

from django.test import TestCase
from django.utils.html import escape

from apps.equipment.tests.helpers import member
from apps.members.models import Member
from apps.payments import checkout, services
from apps.payments.models import Payment

from . import rules
from .models import LockerChange

S = Payment.Source


class BoxFlagTests(TestCase):
    def setUp(self):
        self.trustee = Member.objects.create_superuser(
            "trustee@example.com", "a-good-password", given_name="T", family_name="Rustee"
        )
        self.kim = member(given_name="Kim")

    def test_paying_the_box_fee_sets_the_flag_with_its_date_and_payment(self):
        payment = services.pay_box_fee(self.kim, S.CASH)
        self.kim.refresh_from_db()
        self.assertTrue(self.kim.box_paid)
        self.assertEqual(self.kim.box_payment, payment)
        self.assertEqual(self.kim.box_paid_at, payment.paid_at)
        self.assertEqual(LockerChange.objects.get(what="box-flag").member, self.kim)

    def test_a_member_with_the_flag_is_not_offered_the_fee_again(self):
        self.assertIn("storage-box", [item.key for item in checkout.payables(self.kim)])
        services.pay_box_fee(self.kim, S.CASH)
        self.kim.refresh_from_db()
        self.assertNotIn("storage-box", [item.key for item in checkout.payables(self.kim)])

    def test_the_storage_team_can_tick_a_fee_paid_outside_bbms_but_must_say_how(self):
        with self.assertRaises(rules.LockerError):
            rules.give_box_flag(self.kim, self.trustee)
        self.assertTrue(rules.give_box_flag(self.kim, self.trustee, reason="Paid cash in 2023"))
        self.kim.refresh_from_db()
        self.assertTrue(self.kim.box_paid)
        self.assertIsNone(self.kim.box_payment)
        change = LockerChange.objects.get()
        self.assertEqual(
            (change.reason, change.changed_by, change.new_value), ("Paid cash in 2023", self.trustee, "yes")
        )

    def test_ticking_it_twice_keeps_the_first_date(self):
        rules.give_box_flag(self.kim, self.trustee, reason="Cash")
        self.kim.refresh_from_db()
        first = self.kim.box_paid_at
        self.assertFalse(rules.give_box_flag(self.kim, self.trustee, reason="Cash again"))
        self.kim.refresh_from_db()
        self.assertEqual(self.kim.box_paid_at, first)

    def test_clearing_the_flag_needs_a_reason_and_is_logged(self):
        services.pay_box_fee(self.kim, S.CASH)
        with self.assertRaises(rules.LockerError):
            rules.clear_box_flag(self.kim, "", self.trustee)
        self.assertTrue(rules.clear_box_flag(self.kim, "Set in error", self.trustee))
        self.kim.refresh_from_db()
        self.assertFalse(self.kim.box_paid)
        self.assertIsNone(self.kim.box_payment)
        self.assertEqual(LockerChange.objects.first().reason, "Set in error")

    def test_a_failed_box_payment_takes_the_flag_back(self):
        payment = services.pay_box_fee(self.kim, S.GOCARDLESS, status=Payment.Status.PENDING)
        self.kim.refresh_from_db()
        self.assertTrue(self.kim.box_paid)  # a pending Direct Debit already counts, as for inductions
        rules.box_payment_failed(Payment.objects.filter(pk=payment.pk))
        self.kim.refresh_from_db()
        self.assertFalse(self.kim.box_paid)


class BoxPagesTests(TestCase):
    def setUp(self):
        self.admin = Member.objects.create_superuser(
            "trustee@example.com", "a-good-password", given_name="T", family_name="Rustee"
        )
        self.kim = member(given_name="Kim")
        self.gone = member(given_name="Gone", status=Member.Status.LEFT)
        for who in (self.kim, self.gone):
            services.pay_box_fee(who, S.CASH)
        self.nobody = member(given_name="Nobody")

    def test_the_account_page_shows_whether_a_box_is_paid(self):
        self.client.force_login(self.kim)
        self.assertContains(self.client.get("/account/"), "Label your box with your name")
        self.client.force_login(self.nobody)
        self.assertContains(self.client.get("/account/"), "Pay the box fee")

    def test_the_member_list_filters_by_box(self):
        self.client.force_login(self.admin)
        paid = self.client.get("/admin/members/member/?box=paid").context["cl"].queryset
        self.assertEqual({m.pk for m in paid}, {self.kim.pk, self.gone.pk})
        left = self.client.get("/admin/members/member/?box=left").context["cl"].queryset
        self.assertEqual([m.pk for m in left], [self.gone.pk])

    def test_the_member_page_has_a_box_checkbox_and_wants_a_reason_to_change_it(self):
        from apps.members.admin import MemberChangeForm

        self.client.force_login(self.admin)
        page = self.client.get(f"/admin/members/member/{self.nobody.pk}/change/")
        self.assertContains(page, 'name="box_paid"')
        form = MemberChangeForm(instance=self.nobody, data={"box_paid": "on"})
        form.is_valid()
        self.assertIn("box_reason", form.errors)
        self.assertIn("box_paid", form.changed_data)

    def test_the_checkbox_ticks_and_clears_the_flag_through_the_admin(self):
        from unittest import mock

        from django.contrib.admin.sites import site

        from apps.members.admin import MemberAdmin

        model_admin = MemberAdmin(Member, site)
        request = mock.Mock(user=self.admin)
        ticked = mock.Mock(changed_data=["box_paid"], cleaned_data={"box_paid": True, "box_reason": "Cash, 2023"})
        model_admin.save_model(request, self.nobody, ticked, True)
        self.nobody.refresh_from_db()
        self.assertTrue(self.nobody.box_paid)
        cleared = mock.Mock(changed_data=["box_paid"], cleaned_data={"box_paid": False, "box_reason": "Wrong member"})
        model_admin.save_model(request, self.nobody, cleared, True)
        self.nobody.refresh_from_db()
        self.assertFalse(self.nobody.box_paid)
        reasons = [c.reason for c in LockerChange.objects.filter(member=self.nobody)]
        self.assertEqual(reasons, ["Wrong member", "Cash, 2023"])

    def test_leaving_tells_the_storage_team_about_a_paid_box(self):
        from django.core import mail

        from apps.billing import membership

        team = member(given_name="Stor")
        from django.contrib.auth.models import Group

        Group.objects.get_or_create(name="storage")[0].user_set.add(team)
        membership.become_left(self.kim)
        self.assertTrue(any(escape("box") in m.body.lower() and self.kim.family_name in m.body for m in mail.outbox))
