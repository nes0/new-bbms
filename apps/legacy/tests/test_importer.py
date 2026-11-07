"""The legacy import (spec 08 §8.2), using made-up legacy rows shaped like the real tables."""

from datetime import datetime

from django.contrib.auth import authenticate
from django.test import TestCase

from apps.access import tags
from apps.access.models import AccessEvent, Device, KeyFob
from apps.equipment.models import Equipment, Induction, UsageSession
from apps.legacy.importer import Importer
from apps.legacy.source import ListSource
from apps.members.models import Member

# A real PHP password_hash() of "BBMStest2026!" (as in apps/members/tests.py).
PHP_HASH = "$2y$10$sEjkh81ZV4pWfRu8mhnU4OJk47Ih8KK8f6NibQt4HLuUKJh/DaVWy"
KIM_TAG = tags.full_tag("70", 1234567)  # 700012D68733
SAM_TAG = tags.full_tag("55", 12345601)
DOOR_KEY = "legacy-door-key-1234"
LASER_KEY = "legacy-laser-key-5678"
T = datetime(2025, 3, 1, 12, 0, 0)  # naive, as PyMySQL returns them (UTC)


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


def legacy_tables():
    return {
        "users": [
            user(1, "Kim@Example.com", key_holder=1, trusted=1),
            user(2, "sam@example.com", status="payment-warning"),
            user(3, "admin@example.com"),
            user(4, "kim@example.com"),  # same email as user 1, differently cased
            user(5, "gone@example.com", status="left", password=""),
            user(6, "odd@example.com", status="on-hold"),
        ],
        "profile_data": [
            dict(id=1, user_id=1, profile_photo=1, profile_photo_on_wall=0, new_profile_photo=0),
            dict(id=2, user_id=2, profile_photo=0, profile_photo_on_wall=1, new_profile_photo=1),
            dict(id=3, user_id=99, profile_photo=1, profile_photo_on_wall=0, new_profile_photo=0),
        ],
        "roles": [dict(id=1, name="admin"), dict(id=2, name="laser")],
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
            dict(id=1, user_id=1, key="laser", paid=1, trained=T, trainer_user_id=3, is_trainer=0, created_at=T),
            dict(id=2, user_id=1, key="laser", paid=1, trained=None, trainer_user_id=0, is_trainer=0, created_at=T),
            dict(id=3, user_id=2, key="lathe", paid=1, trained=None, trainer_user_id=0, is_trainer=0, created_at=T),
            dict(id=4, user_id=1, key="Box", paid=1, payment_id=100, trained=None, created_at=T),
            dict(id=5, user_id=2, key="Box", paid=1, payment_id=101, trained=None, created_at=T),
        ],
        "payments": [dict(id=100, status="paid"), dict(id=101, status="failed")],
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

    def test_duplicate_emails_are_skipped_and_reported(self):
        self.assertFalse(Member.objects.filter(legacy_id=4).exists())
        self.assertIn("same email as legacy user 1: 1 (legacy ids 4)", self.output)

    def test_roles(self):
        admin = self.member(3)
        self.assertTrue(admin.is_superuser and admin.is_staff)
        self.assertEqual(list(self.member(1).groups.values_list("name", flat=True)), ["laser"])

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
        self.assertIn("payments (2 rows, payments app)", self.output)
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
