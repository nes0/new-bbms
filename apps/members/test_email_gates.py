"""What needs a confirmed email address, and the admin's power to withdraw or reinstate it (spec 03 §3.1)."""

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.access import tags
from apps.access.decisions import Outcome, decide, door_refusal_reason
from apps.access.models import AccessEvent, Device, KeyFob
from apps.equipment.admin import InductionForm
from apps.equipment.models import Equipment, Induction
from apps.members import changes
from apps.members.models import Member, MemberChange
from apps.notifications.models import Notification

PASSWORD = "a-good-password"


def keyholder(**fields):
    defaults = {
        "given_name": "Kim",
        "family_name": "Keyholder",
        "status": Member.Status.ACTIVE,
        "trusted": True,
        "key_holder": True,
        "photo_approved": True,
        "induction_completed": True,
        "inducted_at": timezone.now(),
        "email_verified_at": timezone.now(),
    }
    return Member.objects.create_user("kim@example.com", PASSWORD, **{**defaults, **fields})


class DoorTests(TestCase):
    def setUp(self):
        self.door = Device.objects.create(name="Front door", device_id="main-door", role=Device.Role.ENTRY)

    def tap(self, member):
        fob = KeyFob.objects.create(member=member, tag_id=tags.full_tag("55", 12345601))
        return decide(self.door, fob.tag_id, fob=fob)

    def test_a_keyholder_with_a_confirmed_email_gets_in(self):
        self.assertEqual(door_refusal_reason(keyholder()), "")

    def test_without_a_confirmed_email_the_door_stays_shut_but_the_visit_is_recorded(self):
        kim = keyholder(email_verified_at=None)
        self.assertEqual(door_refusal_reason(kim), "email_not_confirmed")
        decision = self.tap(kim)
        self.assertEqual((decision.outcome, decision.reason), (Outcome.CHECKED_IN, "email_not_confirmed"))
        self.assertTrue(AccessEvent.objects.filter(member=kim, kind=AccessEvent.Kind.CHECK_IN).exists())

    def test_the_account_page_says_why(self):
        kim = keyholder(email_verified_at=None)
        self.client.force_login(kim)
        self.assertContains(self.client.get("/account/"), "until your email address is confirmed")

    def test_a_member_who_isnt_a_keyholder_is_told_nothing_extra(self):
        self.assertEqual(door_refusal_reason(keyholder(key_holder=False, email_verified_at=None)), "not_keyholder")


class PayingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.kim = keyholder(key_holder=False, trusted=False, email_verified_at=None)
        self.client.force_login(self.kim)

    def test_every_page_that_starts_a_payment_asks_for_confirmation_first(self):
        posts = [
            reverse("payments:pay-card"),
            reverse("payments:pay-credit"),
            reverse("payments:pay-direct-debit"),
            reverse("payments:pay-paypal"),
            reverse("billing:direct-debit-start"),
            reverse("billing:credit-start"),
            reverse("billing:paypal-start"),
            reverse("billing:change-method"),
            reverse("billing:pay-charge", args=[1]),
            reverse("billing:rejoin"),
        ]
        for url in posts:
            response = self.client.post(url, {"item": "top-up", "amount": 10})
            self.assertContains(response, "Confirm your email address first", status_code=403, msg_prefix=url)
        response = self.client.get(reverse("payments:pay"))
        self.assertContains(response, "Confirm your email address first", status_code=403)

    def test_once_confirmed_the_pay_page_opens(self):
        self.kim.email_verified_at = timezone.now()
        self.kim.save()
        self.assertEqual(self.client.get(reverse("payments:pay")).status_code, 200)

    def test_seeing_what_you_owe_and_your_credit_still_works(self):
        self.assertEqual(self.client.get(reverse("billing:payment")).status_code, 200)
        self.assertEqual(self.client.get(reverse("payments:credit")).status_code, 200)

    def test_the_page_offers_a_new_link(self):
        self.assertContains(self.client.get(reverse("payments:pay")), "Email me a new link", status_code=403)


class SignOffTests(TestCase):
    def test_an_unconfirmed_member_cant_be_signed_off(self):
        kim = keyholder(email_verified_at=None)
        Equipment.objects.create(name="Drill", slug="drill", requires_induction=True)
        induction = Induction.objects.create(member=kim, category="drill", paid=True)
        form = InductionForm(
            data={
                "member": kim.pk,
                "category": "drill",
                "paid": "on",
                "trained_at": timezone.localtime().strftime("%Y-%m-%d %H:%M:%S"),
                "created_at": "2026-01-01 10:00:00",
            },
            instance=induction,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("hasn't confirmed their email address", " ".join(form.non_field_errors()))
        kim.email_verified_at = timezone.now()
        kim.save()
        form = InductionForm(data=form.data, instance=Induction.objects.get(pk=induction.pk))
        self.assertTrue(form.is_valid(), form.errors)


class AdminControlTests(TestCase):
    def setUp(self):
        self.admin = Member.objects.create_superuser("admin@example.com", PASSWORD, given_name="A", family_name="D")
        self.client.force_login(self.admin)
        self.kim = keyholder()
        self.list_url = reverse("admin:members_member_changelist")

    def act(self, action, *members):
        return self.client.post(self.list_url, {"action": action, "_selected_action": [m.pk for m in members]})

    def test_an_admin_can_withdraw_confirmation_and_it_is_logged_and_the_member_told(self):
        self.act("revoke_email_confirmation", self.kim)
        self.kim.refresh_from_db()
        self.assertFalse(self.kim.email_verified)
        change = MemberChange.objects.get(member=self.kim, field="email address confirmed")
        self.assertEqual((change.old_value, change.new_value, change.changed_by), ("yes", "no", self.admin))
        (notice,) = Notification.objects.filter(member=self.kim, kind="email_unconfirmed")
        self.assertIn("confirming again", notice.message)
        self.assertEqual(door_refusal_reason(self.kim), "email_not_confirmed")

    def test_and_reinstate_it(self):
        self.act("revoke_email_confirmation", self.kim)
        self.act("reinstate_email_confirmation", self.kim)
        self.kim.refresh_from_db()
        self.assertTrue(self.kim.email_verified)
        values = list(MemberChange.objects.filter(member=self.kim).order_by("pk").values_list("new_value", flat=True))
        self.assertEqual(values, ["no", "yes"])
        self.assertEqual(door_refusal_reason(self.kim), "")

    def test_the_actions_skip_members_already_in_that_state(self):
        self.act("reinstate_email_confirmation", self.kim)  # already confirmed
        self.assertFalse(MemberChange.objects.filter(field="email address confirmed").exists())

    def test_the_member_page_has_a_checkbox_that_does_the_same(self):
        url = reverse("admin:members_member_change", args=[self.kim.pk])
        page = self.client.get(url)
        self.assertContains(page, 'name="email_confirmed"')
        form = page.context["adminform"].form
        self.assertTrue(form.fields["email_confirmed"].initial)

    def test_the_checkbox_saves_through_the_admin(self):
        from unittest import mock

        from django.contrib.admin.sites import site

        from apps.members.admin import MemberAdmin

        form = mock.Mock(changed_data=["email_confirmed"], cleaned_data={"email_confirmed": False})
        MemberAdmin(Member, site).save_model(mock.Mock(user=self.admin), self.kim, form, True)
        self.kim.refresh_from_db()
        self.assertFalse(self.kim.email_verified)
        self.assertTrue(MemberChange.objects.filter(member=self.kim, new_value="no").exists())

    def test_changing_the_address_in_the_admin_clears_it_and_emails_the_new_address_a_link(self):
        from unittest import mock

        from django.contrib.admin.sites import site
        from django.core import mail

        from apps.members.admin import MemberAdmin

        self.kim.email = "typo-fixed@example.com"
        MemberAdmin(Member, site).save_model(
            mock.Mock(user=self.admin), self.kim, mock.Mock(changed_data=["email"], cleaned_data={}), True
        )
        self.kim.refresh_from_db()
        self.assertFalse(self.kim.email_verified)
        self.assertEqual([m.to for m in mail.outbox], [["typo-fixed@example.com"]])
        self.assertTrue(Notification.objects.filter(member=self.kim, kind="email_unconfirmed").exists())

    def test_ticking_it_while_changing_the_address_keeps_it(self):
        from unittest import mock

        from django.contrib.admin.sites import site
        from django.core import mail

        from apps.members.admin import MemberAdmin

        self.kim.email = "verified-by-phone@example.com"
        form = mock.Mock(changed_data=["email", "email_confirmed"], cleaned_data={"email_confirmed": True})
        MemberAdmin(Member, site).save_model(mock.Mock(user=self.admin), self.kim, form, True)
        self.kim.refresh_from_db()
        self.assertTrue(self.kim.email_verified)
        self.assertEqual(mail.outbox, [])

    def test_the_audit_helper_records_it_wherever_it_is_changed(self):
        before = changes.snapshot(self.kim)
        self.kim.email_verified_at = None
        self.kim.save()
        recorded = changes.record(self.kim, before, changed_by=self.admin)
        self.assertEqual([c.field for c in recorded], ["email address confirmed"])
