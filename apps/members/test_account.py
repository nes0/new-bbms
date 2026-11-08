"""The member's own account page (spec 03 §3.1, §3.7–3.8, 02 §2.3)."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.access import tags
from apps.access.models import KeyFob
from apps.equipment.tests.helpers import laser, trained
from apps.payments import services
from apps.payments.models import Payment

from .models import Address, IdDocument, Member


def person(**fields):
    defaults = {"given_name": "Kim", "family_name": "Maker", "phone": "01273 123456", "emergency_contact": "Sam"}
    defaults.update(fields)
    return Member.objects.create_user("kim@example.com", "a-good-password", **defaults)


class AccountPageTests(TestCase):
    def setUp(self):
        self.member = person(status=Member.Status.ACTIVE, monthly_subscription_pence=2500)
        self.client.force_login(self.member)

    def test_needs_a_login(self):
        self.client.logout()
        self.assertEqual(self.client.get("/account/").status_code, 302)

    def test_shows_membership_credit_fobs_and_training(self):
        services.record_payment(self.member, Payment.Reason.BALANCE, Payment.Source.CASH, 1250)
        KeyFob.objects.create(member=self.member, tag_id=tags.full_tag("70", 1234567))
        trained(self.member, laser())
        response = self.client.get("/account/")
        self.assertContains(response, "Active")
        self.assertContains(response, "£25.00")
        self.assertContains(response, "£12.50")
        self.assertContains(response, "0001234567")
        self.assertContains(response, "Red laser")
        self.assertNotContains(response, "Next steps")

    def test_lost_fobs_are_not_listed(self):
        fob = KeyFob.objects.create(member=self.member, card_number=42)
        fob.mark_lost()
        self.assertNotContains(self.client.get("/account/"), "0000000042")

    def test_door_access_is_explained(self):
        self.assertContains(self.client.get("/account/"), "a keyholder lets you in")
        self.member.trusted = self.member.key_holder = True
        self.member.save()
        self.assertContains(self.client.get("/account/"), "until your general induction is confirmed")
        self.member.induction_completed, self.member.inducted_at = True, timezone.now()
        self.member.save()
        self.assertContains(self.client.get("/account/"), "approved photo")
        self.member.photo_approved = True
        self.member.save()
        self.assertContains(self.client.get("/account/"), "Your fob opens the door")

    def test_a_new_member_sees_next_steps(self):
        self.member.status = Member.Status.SETTING_UP
        self.member.save()
        self.assertContains(self.client.get("/account/"), "Next steps")


class EditDetailsTests(TestCase):
    def setUp(self):
        self.member = person()
        self.client.force_login(self.member)

    def test_details_can_be_changed(self):
        data = {"given_name": " Kimberly ", "family_name": "Maker", "phone": "07700 900123", "emergency_contact": "Jo"}
        self.assertRedirects(self.client.post("/account/details/", data), "/account/")
        self.member.refresh_from_db()
        self.assertEqual((self.member.given_name, self.member.phone), ("Kimberly", "07700 900123"))

    def test_everything_is_required_and_the_phone_needs_10_characters(self):
        response = self.client.post(
            "/account/details/", {"given_name": "", "family_name": "M", "phone": "123", "emergency_contact": ""}
        )
        self.assertEqual(set(response.context["form"].errors), {"given_name", "phone", "emergency_contact"})

    def test_other_fields_cannot_be_changed_this_way(self):
        self.client.post(
            "/account/details/",
            {
                "given_name": "Kim",
                "family_name": "Maker",
                "phone": "01273 123456",
                "emergency_contact": "Sam",
                "email": "someone-else@example.com",
                "status": "active",
                "key_holder": "on",
            },
        )
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "kim@example.com")
        self.assertEqual(self.member.status, Member.Status.SETTING_UP)
        self.assertFalse(self.member.key_holder)


class AddressChangeTests(TestCase):
    def setUp(self):
        self.member = person()
        self.old = Address.objects.create(member=self.member, line_1="1 Old Road", postcode="BN1 1AA", approved=True)
        self.client.force_login(self.member)

    def change(self, **data):
        return self.client.post("/account/address/", {"line_1": "2 New Street", "postcode": "bn2 2bb", **data})

    def test_a_change_waits_for_approval_and_the_old_address_is_kept(self):
        self.assertRedirects(self.change(), "/account/")
        self.assertEqual(self.member.current_address, self.old)
        pending = self.member.pending_address
        self.assertEqual((pending.line_1, pending.postcode, pending.approved), ("2 New Street", "BN2 2BB", False))
        self.assertContains(self.client.get("/account/"), "Changing to")

    def test_a_second_change_replaces_the_first(self):
        self.change()
        self.change(line_1="3 Other Lane")
        self.assertEqual(self.member.addresses.filter(approved=False).get().line_1, "3 Other Lane")

    def test_the_postcode_must_be_a_uk_one(self):
        self.assertContains(self.change(postcode="12345"), "Enter a UK postcode")

    def test_approving_replaces_the_old_address(self):
        self.change()
        self.member.pending_address.approve()
        self.assertEqual(self.member.current_address.line_1, "2 New Street")
        self.assertIsNone(self.member.pending_address)
        self.assertEqual(self.member.addresses.count(), 1)

    def test_the_admin_queue_approves_and_declines(self):
        admin = Member.objects.create_superuser("admin@example.com", "a-good-password", given_name="A", family_name="D")
        self.client.force_login(admin)
        self.client.force_login(self.member)
        self.change()
        self.client.force_login(admin)
        pending = self.member.pending_address
        self.client.post("/admin/members/address/", {"action": "decline", "_selected_action": [pending.pk]})
        self.assertIsNone(self.member.pending_address)
        self.assertEqual(self.member.current_address, self.old)

        self.client.force_login(self.member)
        self.change()
        self.client.force_login(admin)
        pending = self.member.pending_address
        self.client.post("/admin/members/address/", {"action": "approve", "_selected_action": [pending.pk]})
        self.assertEqual(self.member.current_address.line_1, "2 New Street")

    def test_a_new_members_registration_address_shows_as_waiting(self):
        self.old.delete()
        Address.objects.create(member=self.member, line_1="1 Join Street", postcode="BN1 4AA")
        self.assertContains(self.client.get("/account/"), "Waiting for a trustee to check")


class SubscriptionTests(TestCase):
    def setUp(self):
        self.member = person(monthly_subscription_pence=2000)
        self.client.force_login(self.member)

    def test_the_amount_can_be_changed(self):
        self.assertRedirects(self.client.post("/account/subscription/", {"monthly_subscription": "30"}), "/account/")
        self.member.refresh_from_db()
        self.assertEqual(self.member.monthly_subscription_pence, 3000)

    def test_at_least_the_minimum_in_whole_pounds(self):
        for bad in ("24", "25.50", ""):
            self.client.post("/account/subscription/", {"monthly_subscription": bad})
            self.member.refresh_from_db()
            self.assertEqual(self.member.monthly_subscription_pence, 2000, bad)

    def test_a_member_paying_less_than_a_new_minimum_keeps_their_amount_until_they_change_it(self):
        # This member chose £20 under the old minimum; raising it to £25 doesn't change their amount.
        self.assertContains(self.client.get("/account/"), "£20.00")
        self.assertContains(self.client.get("/account/subscription/"), "at least £25")


class PasswordChangeTests(TestCase):
    def test_uses_the_sites_own_page_and_works(self):
        member = person()
        self.client.force_login(member)
        self.assertContains(self.client.get("/accounts/password_change/"), "Change your password")
        response = self.client.post(
            "/accounts/password_change/",
            {
                "old_password": "a-good-password",
                "new_password1": "solder-and-sawdust",
                "new_password2": "solder-and-sawdust",
            },
            follow=True,
        )
        self.assertContains(response, "Your password has been changed")
        member.refresh_from_db()
        self.assertTrue(member.check_password("solder-and-sawdust"))


class LeaveTests(TestCase):
    def setUp(self):
        self.member = person()
        self.client.force_login(self.member)

    def test_a_setting_up_account_is_deleted_with_its_uploads(self):
        Address.objects.create(member=self.member, line_1="1 Join Street", postcode="BN1 4AA")
        IdDocument.objects.create(
            member=self.member,
            kind="photo_id",
            filename="p.jpg",
            content_type="image/jpeg",
            size=3,
            data=b"\xff\xd8\xff",
        )
        response = self.client.post("/account/leave/", {"confirm": "on"})
        self.assertRedirects(response, "/")
        self.assertFalse(Member.objects.exists())
        self.assertFalse(Address.objects.exists())
        self.assertFalse(IdDocument.objects.exists())
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_it_must_be_confirmed(self):
        self.client.post("/account/leave/", {})
        self.assertTrue(Member.objects.exists())

    def test_an_account_with_payments_is_not_deleted(self):
        services.record_payment(self.member, Payment.Reason.BALANCE, Payment.Source.CASH, 500)
        response = self.client.post("/account/leave/", {"confirm": "on"}, follow=True)
        self.assertContains(response, "contact the trustees")
        self.assertTrue(Member.objects.exists())

    def test_a_current_member_leaves_when_their_paid_time_runs_out(self):
        # The billing app's tests cover the Direct Debit being cancelled (apps/billing/tests).
        self.member.status = Member.Status.ACTIVE
        self.member.save()
        self.assertContains(self.client.get("/account/leave/"), "stop your monthly payments")
        self.client.post("/account/leave/", {})  # not confirmed
        self.member.refresh_from_db()
        self.assertEqual(self.member.status, Member.Status.ACTIVE)
        self.client.post("/account/leave/", {"confirm": "on"})
        self.member.refresh_from_db()
        self.assertEqual(self.member.status, Member.Status.LEAVING)


class PendingAddressTests(TestCase):
    def test_an_older_unapproved_address_is_not_pending(self):
        member = person()
        Address.objects.create(
            member=member, line_1="Old", postcode="BN1 1AA", created_at=timezone.now() - timedelta(days=2)
        )
        Address.objects.create(member=member, line_1="New", postcode="BN1 1AA", approved=True)
        self.assertIsNone(member.pending_address)
