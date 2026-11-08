"""The legacy import (spec 08 §8.2), using made-up legacy rows shaped like the real tables."""

import hashlib
import tempfile
import urllib.error
from datetime import datetime

from django.contrib.auth import authenticate
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings

from apps.access import tags
from apps.access.models import AccessEvent, Device, KeyFob
from apps.equipment.models import Equipment, Induction, UsageSession
from apps.legacy.importer import Importer
from apps.legacy.photos import PNG_SIGNATURE, PhotoSource
from apps.legacy.source import ListSource
from apps.members.models import Member, MemberChange, MemberPhoto
from apps.notifications.models import Notification
from apps.payments.credit import credit_pence
from apps.payments.models import Payment

# A real PHP password_hash() of "BBMStest2026!" (as in apps/members/tests.py).
PHP_HASH = "$2y$10$sEjkh81ZV4pWfRu8mhnU4OJk47Ih8KK8f6NibQt4HLuUKJh/DaVWy"
KIM_TAG = tags.full_tag("70", 1234567)  # 700012D68733, a made-up fob
SAM_TAG = tags.full_tag("55", 12345601)
DOOR_KEY = "legacy-door-key-1234"
LASER_KEY = "legacy-laser-key-5678"
T = datetime(2025, 3, 1, 12, 0, 0)  # naive, as PyMySQL returns them (UTC)
T2 = datetime(2025, 6, 1, 12, 0, 0)
T3 = datetime(2025, 9, 1, 12, 0, 0)


def user(id, email, status="active", **fields):
    row = dict(
        id=id,
        email=email,
        given_name=f"Given{id}",
        family_name=f"Family{id}",
        password=PHP_HASH,
        status=status,
        phone="",
        emergency_contact="",
        trusted=0,
        key_holder=0,
        founder=0,
        induction_completed=1,
        rules_agreed=T,
        profile_private=0,
        banned=0,
        banned_date=None,
        banned_reason="",
        notes="",
        created_at=T,
    )
    row.update(fields)
    return row


def payment(id, user_id, reason, source, amount, *, fee=0.0, ref="", source_id="", status="paid"):
    return dict(
        id=id,
        user_id=user_id,
        reason=reason,
        reference=ref,
        source=source,
        source_id=source_id,
        amount=amount,
        fee=fee,
        amount_minus_fee=amount - fee,
        status=status,
        paid_at=T if status == "paid" else "0000-00-00 00:00:00",
        created_at=T,
    )


def legacy_tables():
    return {
        "users": [
            user(
                1,
                "Kim@Example.com",
                key_holder=1,
                trusted=1,
                payment_method="gocardless-variable",
                payment_day=31,
                monthly_subscription=25,
                mandate_id="MD-MADE-UP-1",
                subscription_expires=datetime(2025, 4, 1),
                inducted_by=3,
                cash_balance=700,  # legacy's cached credit, in pence: £10 top-up − £3 laser fee
                hash="made-up-hash-kim",
            ),
            user(
                2,
                "sam@example.com",
                status="payment-warning",
                payment_method="paypal",
                secondary_email="Sam.PayPal@example.com",
                monthly_subscription=20,
                cash_balance=13,
                hash="made-up-hash-sam",
            ),
            user(3, "admin@example.com", payment_method="balance", payment_day=0, cash_balance=0),  # really -5p
            user(4, "kim@example.com"),  # same email as user 1, differently cased
            user(5, "gone@example.com", status="left", password=""),
            # Confirmed by someone not imported, so the door needs it confirming again.
            user(6, "odd@example.com", status="on-hold", inducted_by=99, trusted=1, key_holder=1),
        ],
        "user_address": [
            # Kim: an old approved address, the current one, and a change waiting for approval
            dict(id=1, user_id=1, line_1="1 Old Street", postcode="BN1 1AA", approved=1, created_at=T),
            dict(id=2, user_id=1, line_1="2 Current Street", line_2="", postcode="bn2 2bb", approved=1, created_at=T2),
            dict(id=3, user_id=1, line_1="3 New Street", postcode="BN3 3CC", approved=0, created_at=T3),
            # Sam: a change older than the approved address (out of date)
            dict(id=4, user_id=2, line_1="4 Stale Street", postcode="BN4 4DD", approved=0, created_at=T),
            dict(id=5, user_id=2, line_1="5 Real Street", postcode="BN5 5EE", approved=1, created_at=T2),
            dict(id=6, user_id=42, line_1="6 Nobody Street", postcode="BN6 6FF", approved=1, created_at=T),
            dict(id=7, user_id=3, line_1="", postcode="", approved=1, created_at=T),
        ],
        "audit_log": [
            dict(id=1, user_id=1, admin_id=3, action="user_updated", description="trusted", created_at=T),
            dict(id=2, user_id=42, admin_id=3, action="user_updated", description="key_holder", created_at=T),
        ],
        "notifications": [
            dict(
                id=1,
                user_id=1,
                type="trusted",
                message="You are now trusted",
                hash="trusted_status",
                unread=0,
                notified_at=T,
                created_at=T2,
            ),
            dict(
                id=2,
                user_id=1,
                type="trusted",
                message="Sent again",
                hash="trusted_status",
                unread=1,
                notified_at=T2,
                created_at=T2,
            ),
            dict(id=3, user_id=2, type="x", message="Something", hash="", unread=1, notified_at=None, created_at=T3),
            dict(id=4, user_id=42, type="x", message="Nobody", hash="", unread=1, notified_at=T, created_at=T),
        ],
        "subscription_charge": [
            dict(
                id=1,
                user_id=1,
                charge_date=datetime(2025, 3, 1),
                payment_date=datetime(2025, 3, 4),
                amount=2500,
                status="paid",
                created_at=T,
            ),
            dict(
                id=2,
                user_id=1,
                charge_date=datetime(2025, 4, 1),
                payment_date=None,
                amount=0,
                status="pending",
                created_at=T,
            ),
            dict(
                id=3,
                user_id=1,
                charge_date=datetime(2025, 4, 1),
                payment_date=None,
                amount=0,
                status="due",
                created_at=T,
            ),
            dict(id=4, user_id=99, charge_date=datetime(2025, 4, 1), amount=0, status="due", created_at=T),
            dict(id=5, user_id=3, charge_date=datetime(2025, 4, 1), amount=0, status="weird", created_at=T),
        ],
        "profile_data": [
            dict(
                id=1,
                user_id=1,
                profile_photo=1,
                profile_photo_on_wall=0,
                new_profile_photo=0,
                profile_photo_private=1,
                tagline="Makes things",
                description="Lasers and lathes.",
                skills_array='["laser-cutter", "welding", "juggling"]',
                github="https://github.com/made-up-kim",
                twitter="@madeupkim",
                google_plus="kim",
                website="example.com",
            ),
            dict(id=2, user_id=2, profile_photo=0, profile_photo_on_wall=1, new_profile_photo=1),
            dict(id=3, user_id=99, profile_photo=1, profile_photo_on_wall=0, new_profile_photo=0),
        ],
        "roles": [
            dict(id=1, name="admin"),
            dict(
                id=2,
                name="laser",
                title="Laser team",
                description="Look after the laser.",
                email_public="laser@example.com",
            ),
            dict(id=3, name="comms"),
        ],
        "role_user": [
            dict(id=1, role_id=1, user_id=3),
            dict(id=2, role_id=2, user_id=1),
            dict(id=3, role_id=2, user_id=1),  # repeated
        ],
        "key_fobs": [
            dict(id=1, user_id=1, key_id=KIM_TAG.lower(), active=1, lost=0, created_at=T, updated_at=T),
            dict(id=2, user_id=2, key_id=SAM_TAG, active=1, lost=0, created_at=T, updated_at=T),
            dict(id=3, user_id=2, key_id="BB1234567890", active=0, lost=1, created_at=T, updated_at=T),
            dict(id=4, user_id=5, key_id=KIM_TAG, active=0, lost=0, created_at=T, updated_at=T),  # clashes with 1
            dict(id=5, user_id=42, key_id="AABBCCDDEEFF", active=1, lost=0, created_at=T, updated_at=T),
        ],
        "acs_nodes": [
            dict(
                id=1,
                name="Front door",
                device_id="main-door",
                entry_device=1,
                api_key=DOOR_KEY,
                monitor_heartbeat=1,
                last_boot=None,
                last_heartbeat=T,
            ),
            dict(
                id=2,
                name="",
                device_id="laser",
                entry_device=0,
                api_key=LASER_KEY,
                monitor_heartbeat=0,
                last_boot=None,
                last_heartbeat=None,
            ),
            dict(
                id=3,
                name="Spare",
                device_id="spare",
                entry_device=0,
                api_key=None,
                monitor_heartbeat=0,
                last_boot=None,
                last_heartbeat=None,
            ),
        ],
        "equipment": [
            dict(
                id=2,
                name="Laser Cutter",
                slug="laser",
                device_key="laser",
                induction_category="laser",
                requires_induction=1,
                access_fee=10,
                usage_cost=300,
                usage_cost_per="hour",
                managing_role_id=2,
                working=1,
                archive=0,
                permaloan=0,
                permaloan_user_id=None,
                obtained_at="0000-00-00",
                removed_at="0000-00-00",
                ppe='["eye-protection"]',
                updated_at=T,
            ),
            dict(
                id=3,
                name="Big Printer",
                slug="",
                induction_category=None,
                requires_induction=0,
                access_fee=0,
                usage_cost=5,
                usage_cost_per="gram",
                managing_role_id=None,
                working=0,
                archive=0,
                permaloan=1,
                permaloan_user_id=2,
                obtained_at=T.date(),
                removed_at=None,
                ppe=None,
                updated_at=T,
            ),
            dict(
                id=41,
                name="Member Box",
                slug="19l-box",
                induction_category="Box",
                requires_induction=1,
                access_fee=15,
                usage_cost=0,
                usage_cost_per=None,
                working=1,
                archive=0,
                updated_at=T,
            ),
        ],
        "inductions": [
            dict(
                id=1,
                user_id=1,
                key="laser",
                paid=1,
                payment_id=106,
                trained=T,
                trainer_user_id=3,
                is_trainer=0,
                created_at=T,
            ),
            dict(id=2, user_id=1, key="laser", paid=1, trained=None, trainer_user_id=0, is_trainer=0, created_at=T),
            dict(id=3, user_id=2, key="lathe", paid=1, trained=None, trainer_user_id=0, is_trainer=0, created_at=T),
            dict(id=4, user_id=1, key="Box", paid=1, payment_id=100, trained=None, created_at=T),
            dict(id=5, user_id=2, key="Box", paid=1, payment_id=101, trained=None, created_at=T),
        ],
        "payments": [
            payment(100, 1, "storage-box", "cash", 15.0),
            payment(101, 2, "storage-box", "gocardless-variable", 15.0, status="failed"),
            payment(102, 1, "subscription", "gocardless-variable", 25.0, fee=0.2, ref="1", source_id="PM-MADE-UP-1"),
            payment(103, 1, "balance", "stripe", 10.0, fee=0.35, source_id="ch_made_up"),
            payment(104, 1, "equipment-fee", "balance", 3.0, ref="1:laser"),
            payment(105, 3, "printer", "balance", 0.05, ref="printer-1"),
            payment(106, 1, "induction", "gocardless-variable", 10.0, ref="laser", status="pending_submission"),
            payment(107, 0, "donation", "paypal", 5.0),
            payment(108, 42, "balance", "cash", 1.0),
            payment(109, 2, "balance", "gocardless-variable", 9.0, status="weird"),
            payment(110, 2, "balance", "cash", 0.125),  # rounds half up to 13p
        ],
        "equipment_log": [
            # billed hour on the laser
            dict(
                id=1,
                user_id=1,
                key_fob_id=1,
                device="laser",
                started=T,
                last_update=None,
                finished=datetime(2025, 3, 1, 13, 0),
                removed=0,
                billed=1,
                reason="",
            ),
            # never billed by the legacy system
            dict(
                id=2,
                user_id=1,
                key_fob_id=1,
                device="laser",
                started=T,
                last_update=None,
                finished=datetime(2025, 3, 1, 12, 30),
                removed=0,
                billed=0,
                reason="",
            ),
            dict(
                id=3,
                user_id=1,
                key_fob_id=1,
                device="laser",
                started=T,
                last_update=None,
                finished=datetime(2025, 3, 1, 14, 0),
                removed=0,
                billed=0,
                reason="training",
            ),
            dict(
                id=4,
                user_id=1,
                key_fob_id=1,
                device="laser",
                started=T,
                last_update=T,
                finished=None,
                removed=0,
                billed=0,
                reason="",
            ),
            dict(id=5, user_id=1, key_fob_id=1, device="spark", started=T, finished=T, removed=0, billed=0, reason=""),
        ],
        "access_log": [
            dict(id=1, user_id=1, key_fob_id=1, response="200", service="main-door", delayed=0, created_at=T),
            dict(id=2, user_id=2, key_fob_id=2, response="402", service="main-door", delayed=1, created_at=T),
            dict(id=3, user_id=2, key_fob_id=2, response="200", service="old-cctv", delayed=0, created_at=T),
        ],
    }


class ImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.importer = Importer(ListSource(legacy_tables()))
        cls.report = cls.importer.run()
        cls.output = "\n".join(cls.report.lines())

    def member(self, legacy_id):
        return Member.objects.get(legacy_id=legacy_id)

    def test_members_keep_their_passwords(self):
        kim = authenticate(email="kim@example.com", password="BBMStest2026!")
        self.assertEqual(kim, self.member(1))
        self.assertFalse(self.member(5).has_usable_password())

    def test_member_fields_and_statuses(self):
        kim = self.member(1)
        self.assertEqual(kim.email, "kim@example.com")
        self.assertTrue(kim.key_holder and kim.trusted and kim.photo_approved)
        self.assertEqual(kim.date_joined.isoformat(), "2025-03-01T12:00:00+00:00")
        self.assertEqual(self.member(2).status, Member.Status.ACTIVE)  # payment-warning
        self.assertTrue(self.member(2).photo_on_wall)
        self.assertEqual(self.member(6).status, Member.Status.SUSPENDED)  # on-hold

    def test_how_members_pay(self):
        kim = self.member(1)
        self.assertEqual(
            (kim.payment_method, kim.payment_day, kim.monthly_subscription_pence, kim.mandate_id),
            ("gocardless-variable", 1, 2500, "MD-MADE-UP-1"),  # day 31 becomes the 1st, as in legacy
        )
        self.assertEqual(kim.subscription_expires.isoformat(), "2025-04-01")
        self.assertEqual((self.member(3).payment_method, self.member(3).payment_day), ("balance", None))
        sam = self.member(2)
        self.assertEqual((sam.payment_method, sam.paypal_email), ("paypal", "sam.paypal@example.com"))
        self.assertNotIn("paying by 'paypal'", self.output)

    def test_profiles(self):
        from apps.profiles.models import Profile, RoleInfo

        kim = Profile.objects.get(member=self.member(1))
        self.assertEqual(
            (kim.tagline, kim.skills, kim.photo_private), ("Makes things", ["laser-cutter", "welding"], True)
        )
        self.assertEqual((kim.github, kim.twitter, kim.website), ("made-up-kim", "madeupkim", "https://example.com"))
        self.assertEqual(Profile.objects.count(), Member.objects.count())  # everyone has one
        self.assertIn("skills not on the list, left out: juggling (1)", self.output)
        laser = RoleInfo.objects.get(group__name="laser")
        self.assertEqual((laser.title, laser.email), ("Laser team", "laser@example.com"))

    def test_subscription_charges(self):
        from apps.billing.models import Charge

        paid = Charge.objects.get(legacy_id=1)
        self.assertEqual((paid.status, paid.amount_pence, paid.paid_on.isoformat()), ("paid", 2500, "2025-03-04"))
        self.assertEqual(Charge.objects.get(legacy_id=2).amount_pence, None)
        self.assertIn("a second charge for the same member and date: 1 (legacy ids 3)", self.output)
        self.assertIn("no such member: 1 (legacy ids 4)", self.output)
        self.assertIn("unknown status 'weird': 1 (legacy ids 5)", self.output)

    def test_duplicate_emails_are_skipped_and_reported(self):
        self.assertFalse(Member.objects.filter(legacy_id=4).exists())
        self.assertIn("same email as legacy user 1: 1 (legacy ids 4)", self.output)

    def test_roles(self):
        admin = self.member(3)
        self.assertTrue(admin.is_superuser and admin.is_staff)
        self.assertEqual(list(self.member(1).groups.values_list("name", flat=True)), ["laser"])

    def test_general_inductions_and_the_comms_role(self):
        self.assertEqual(self.member(1).inducted_by, self.member(3))
        self.assertEqual(self.member(1).general_induction_status, "confirmed")
        self.assertEqual(self.member(2).general_induction_status, "waiting")
        self.assertEqual(self.member(6).general_induction_status, "waiting")
        self.assertIn("1 general induction(s) confirmed by someone not imported", self.output)
        self.assertIn("1 key holder(s) without a confirmed general induction", self.output)
        self.assertIn("(legacy ids 6)", self.output)
        comms = Group.objects.get(name="comms")
        self.assertTrue(comms.permissions.filter(codename="confirm_general_inductions").exists())

    def test_audit_log(self):
        change = MemberChange.objects.get()
        self.assertEqual((change.member, change.changed_by), (self.member(1), self.member(3)))
        self.assertEqual((change.field, change.new_value), ("legacy", "trusted"))
        self.assertEqual(change.changed_at.isoformat(), "2025-03-01T12:00:00+00:00")
        self.assertIn("no such member: 1 (legacy ids 2)", self.output)

    def test_notifications(self):
        read, waiting = Notification.objects.order_by("legacy_id")
        self.assertEqual((read.member, read.key, read.unread), (self.member(1), "trusted_status", False))
        self.assertEqual(read.message, "You are now trusted")
        self.assertEqual(read.created_at.isoformat(), "2025-03-01T12:00:00+00:00")
        self.assertEqual((waiting.member, waiting.key, waiting.unread), (self.member(2), "", True))
        self.assertEqual(waiting.created_at.isoformat(), "2025-09-01T12:00:00+00:00")  # no notified_at: created_at
        self.assertIn("no such member: 1 (legacy ids 4)", self.output)
        self.assertIn("the same notification twice for a member: 1 (legacy ids 2)", self.output)

    def test_fobs(self):
        kim = KeyFob.objects.get(legacy_id=1)
        self.assertEqual((kim.tag_id, kim.card_number, kim.active), ("700012D68733", 1234567, True))
        old = KeyFob.objects.get(legacy_id=3)
        self.assertFalse(old.active)
        self.assertIsNotNone(old.lost_at)
        self.assertFalse(KeyFob.objects.filter(legacy_id=4).exists())  # clashed with the active fob 1
        self.assertIn("same tag as another fob: 1 (legacy ids 4)", self.output)
        self.assertIn("no such member: 1 (legacy ids 5)", self.output)

    def test_devices_keep_their_api_keys(self):
        self.assertEqual(Device.for_api_key(DOOR_KEY).role, Device.Role.ENTRY)
        self.assertEqual(Device.for_api_key(LASER_KEY).role, Device.Role.EQUIPMENT)
        self.assertIn("device 3 (spare) had no API key", self.output)
        self.assertIn("equipment device 3 (spare) matches no equipment slug", self.output)

    def test_equipment(self):
        laser = Equipment.objects.get(slug="laser")
        self.assertEqual((laser.induction_fee_pence, laser.usage_cost_pence_per_hour), (1000, 300))
        self.assertEqual(laser.team.name, "laser")
        self.assertEqual(laser.ppe, "eye-protection")
        self.assertIsNone(laser.obtained_at)
        printer = Equipment.objects.get(legacy_id=3)
        self.assertEqual(printer.slug, "big-printer")
        self.assertEqual(printer.usage_cost_pence_per_hour, 0)  # per gram isn't supported
        self.assertEqual(printer.status, Equipment.Status.OUT_OF_ORDER)
        self.assertEqual(printer.permaloan_member, self.member(2))
        self.assertFalse(Equipment.objects.filter(legacy_id=41).exists())  # Member Box

    def test_inductions(self):
        kim_laser = Induction.objects.get(member=self.member(1), category="laser")
        self.assertEqual(kim_laser.legacy_id, 1)  # the trained one wins over the duplicate
        self.assertTrue(kim_laser.is_trained)
        self.assertEqual(kim_laser.trainer, self.member(3))
        self.assertFalse(Induction.objects.filter(category__iexact="box").exists())
        self.assertIn("inductions for categories no equipment uses: lathe 1", self.output)

    def test_addresses_keep_the_current_one_and_a_waiting_change(self):
        kim = self.member(1)
        self.assertEqual(kim.current_address.line_1, "2 Current Street")
        self.assertEqual(kim.current_address.postcode, "BN2 2BB")
        self.assertEqual(kim.pending_address.line_1, "3 New Street")
        sam = self.member(2)
        self.assertEqual([a.line_1 for a in sam.addresses.all()], ["5 Real Street"])
        self.assertIn("an older approved address (replaced): 1 (legacy ids 1)", self.output)
        self.assertIn("a change older than the approved address: 1 (legacy ids 4)", self.output)
        self.assertIn("no first line: 1 (legacy ids 7)", self.output)
        self.assertIn("1 address change(s) waiting for approval", self.output)

    def test_payments_in_pence_with_their_reasons_sources_and_statuses(self):
        subscription = Payment.objects.get(legacy_id=102)
        self.assertEqual(
            (subscription.reason, subscription.source, subscription.amount_pence, subscription.fee_pence),
            ("subscription", "gocardless-variable", 2500, 20),
        )
        self.assertEqual(subscription.source_id, "PM-MADE-UP-1")
        self.assertEqual(subscription.paid_at.isoformat(), "2025-03-01T12:00:00+00:00")
        self.assertEqual(Payment.objects.get(legacy_id=110).amount_pence, 13)  # £0.125, half up
        induction_fee = Payment.objects.get(legacy_id=106)
        self.assertEqual((induction_fee.status, induction_fee.paid_at), ("pending", None))
        printer = Payment.objects.get(legacy_id=105)
        self.assertEqual((printer.reason, printer.reference), ("consumable", "printer:printer-1"))
        self.assertIsNone(Payment.objects.get(legacy_id=107).member)  # a donation from someone outside
        self.assertIsNone(Payment.objects.get(legacy_id=108).member)
        self.assertEqual(Payment.objects.get(legacy_id=109).status, "cancelled")
        self.assertIn("payments for other services, imported as consumables: printer 1", self.output)
        self.assertIn("payments with unknown statuses, imported as cancelled", self.output)
        self.assertIn("1 payment(s) of members not imported kept with no member", self.output)

    def test_payments_are_linked_to_charges_sessions_and_inductions(self):
        from apps.billing.models import Charge

        self.assertEqual(Payment.objects.get(legacy_id=102).charge, Charge.objects.get(legacy_id=1))
        self.assertEqual(Payment.objects.get(legacy_id=104).usage_session, UsageSession.objects.get(legacy_id=1))
        self.assertEqual(Induction.objects.get(legacy_id=1).payment, Payment.objects.get(legacy_id=106))

    def test_credit_is_checked_against_legacy(self):
        self.assertEqual(credit_pence(self.member(1)), 700)
        self.assertEqual(credit_pence(self.member(2)), 13)
        self.assertIn("credit differs from legacy's cached balance for 1 member(s), by £-0.05", self.output)
        self.assertIn("(legacy ids, new minus legacy: 3 (-5p))", self.output)

    def test_photos_are_only_copied_when_asked(self):
        self.assertFalse(MemberPhoto.objects.exists())
        self.assertIn("photos not copied (2 to copy): run with --photos-from", self.output)

    def test_box_paid_flags(self):
        self.assertEqual(self.importer.box_paid_user_ids, [1])
        self.assertIn("box paid: 1 members qualify; 1 have a paid induction but no paid payment", self.output)

    def test_sessions(self):
        billed = UsageSession.objects.get(legacy_id=1)
        self.assertEqual((billed.fee_pence, billed.charged_at), (300, billed.ended_at))
        unbilled = UsageSession.objects.get(legacy_id=2)
        self.assertIsNotNone(unbilled.charged_at)  # never charged again by the new system
        self.assertEqual(UsageSession.objects.get(legacy_id=3).fee_pence, 0)  # training
        self.assertIsNone(UsageSession.objects.get(legacy_id=4).ended_at)
        self.assertFalse(UsageSession.objects.filter(legacy_id=5).exists())
        self.assertIn("1 finished sessions the legacy system hadn't billed would cost £1.50", self.output)
        self.assertIn("sessions on unknown devices: spark 1", self.output)

    def test_door_log(self):
        self.assertEqual(AccessEvent.objects.filter(kind=AccessEvent.Kind.ENTRY).count(), 2)
        check_in = AccessEvent.objects.get(kind=AccessEvent.Kind.CHECK_IN)
        self.assertEqual((check_in.member, check_in.delayed), (self.member(2), True))
        self.assertIn("door log services with no device: old-cctv 1", self.output)

    def test_report_lists_tables_not_imported_and_shows_no_personal_data(self):
        self.assertIn("proposals (0 rows, governance app)", self.output)
        self.assertNotIn("Street", self.output)
        self.assertNotIn("@example.com", self.output)
        self.assertNotIn("Given", self.output)


class ImportedDataWorksTests(TestCase):
    """After the import, today's devices carry on working with their old keys and fobs."""

    def setUp(self):
        Importer(ListSource(legacy_tables())).run()

    def tap(self, key, tag, device):
        return self.client.post(
            "/acs/activity",
            {"tagId": tag, "device": device},
            content_type="application/json",
            headers={"ApiKey": key},
        )

    def test_keyholder_opens_the_door(self):
        Member.objects.filter(legacy_id=1).update(photo_approved=True)
        self.assertEqual(self.tap(DOOR_KEY, KIM_TAG, "main-door").status_code, 201)

    def test_trained_member_starts_the_laser(self):
        UsageSession.objects.filter(legacy_id=4).delete()  # the session left open by the legacy system
        response = self.tap(LASER_KEY, KIM_TAG, "laser")
        self.assertEqual(response.status_code, 201, response.content)


PHOTO_FOLDER = "https://photos.example.com/bucket/production"


def fake_s3(files):
    """Stands in for legacy's public S3 folder: {name: bytes}."""

    def opener(url):
        name = url.removeprefix(PHOTO_FOLDER + "/user-photo/")
        if name not in files:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        return files[name]

    return opener


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="bbms-test-media-"))
class ImportOptionsTests(TestCase):
    def setUp(self):
        kim = hashlib.md5(b"made-up-hash-kim").hexdigest()
        sam = hashlib.md5(b"made-up-hash-sam").hexdigest()
        self.files = {
            f"{kim}.png": PNG_SIGNATURE + b"kim-500",
            f"{kim}-thumb.png": PNG_SIGNATURE + b"kim-200",
            f"{sam}-new.png": PNG_SIGNATURE + b"sam-500",
            f"{sam}-thumb-new.png": b"<html>not a photo</html>",
        }

    def run_import(self, save=True, **options):
        photos = PhotoSource(PHOTO_FOLDER, save=save, opener=fake_s3(self.files))
        report = Importer(ListSource(legacy_tables()), photos=photos, **options).run()
        return "\n".join(report.lines())

    def test_photos_are_copied_to_private_storage_under_new_names(self):
        output = self.run_import()
        photo = MemberPhoto.objects.get()
        self.assertEqual((photo.member.legacy_id, photo.kind), (1, "current"))
        self.assertEqual(photo.image.read(), PNG_SIGNATURE + b"kim-500")
        self.assertEqual(photo.thumbnail.read(), PNG_SIGNATURE + b"kim-200")
        self.assertNotIn("made-up-hash", photo.image.name)
        self.assertIn("photos copied: 1 approved, 0 waiting for approval", output)
        self.assertIn("1 photo(s) couldn't be fetched (legacy user ids 2)", output)  # Sam's thumbnail isn't a PNG

    def test_a_dry_run_only_checks_the_photos(self):
        output = self.run_import(save=False)
        self.assertFalse(MemberPhoto.objects.exists())
        self.assertIn("photos found (dry run: not copied): 1 approved", output)

    def test_photos_must_come_over_https(self):
        with self.assertRaises(ValueError):
            PhotoSource("http://photos.example.com/bucket/production")

    def test_mark_inducted_confirms_everyone_who_has_been_a_member(self):
        output = self.run_import(mark_inducted=True)
        for legacy_id in (1, 2, 3, 5, 6):
            self.assertEqual(Member.objects.get(legacy_id=legacy_id).general_induction_status, "confirmed", legacy_id)
        self.assertEqual(Member.objects.get(legacy_id=1).inducted_by, Member.objects.get(legacy_id=3))  # kept
        change = MemberChange.objects.get(member__legacy_id=6, field="induction confirmed by")
        self.assertEqual(change.new_value, "the legacy import (--mark-inducted)")
        self.assertIn("--mark-inducted: 4 member(s) given a confirmed general induction", output)
        self.assertNotIn("key holder(s) without a confirmed general induction", output)
