"""Settings the trustees change in the admin, and the places that use them (spec 02 §2.8)."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.access.models import Device
from apps.equipment import rules
from apps.equipment.models import UsageSession
from apps.equipment.tests.helpers import laser, member, trained
from apps.members import id_documents
from apps.members.models import IdDocument, Member
from apps.payments import services
from apps.payments.models import Payment

from .models import SettingChange, SiteSettings

URL = "/admin/site_settings/sitesettings/"
DEFAULTS = {
    "space_name": "Build Brighton",
    "minimum_subscription_pounds": "25",
    "key_deposit_pounds": "15.00",
    "box_fee_pounds": "15.00",
    "top_up_minimum_pounds": "5.00",
    "top_up_maximum_pounds": "200.00",
    "minimum_session_minutes": "15",
    "machine_credit_limit_pounds": "5.00",
    "reader_check_in_seconds": "300",
    "session_heartbeat_seconds": "60",
    "id_document_max_age_days": "30",
    "lockers_per_member": "1",
    "admin_minimum_subscription_pounds": "5",
    "grace_direct_debit_days": "10",
    "grace_standing_order_days": "183",
    "grace_other_days": "7",
    "suspended_to_left_days": "30",
    "dd_retry_mode": "bbms",
    "dd_retries": "1",
    "dd_retry_after_days": "5",
    "max_active_fobs": "2",
    "fob_enrolment_seconds": "60",
}


def change(**values):
    SiteSettings.objects.update_or_create(pk=1, defaults=values)


class SiteSettingsAdminTests(TestCase):
    def setUp(self):
        self.admin = Member.objects.create_superuser(
            "admin@example.com", "a-good-password", given_name="A", family_name="D"
        )
        self.client.force_login(self.admin)

    def post(self, **changes):
        return self.client.post(f"{URL}1/change/", {**DEFAULTS, **changes})

    def test_the_starting_values_are_the_legacy_ones_except_the_new_minimum(self):
        row = SiteSettings.load()
        self.assertEqual(
            (row.minimum_subscription_pence, row.key_deposit_pence, row.box_fee_pence, row.minimum_session_minutes),
            (2500, 1500, 1500, 15),
        )
        self.assertEqual((row.machine_credit_limit_pence, row.id_document_max_age_days), (500, 30))

    def test_there_is_only_one_row(self):
        SiteSettings.load()
        SiteSettings().save()
        self.assertEqual(SiteSettings.objects.count(), 1)

    def test_the_admin_list_goes_straight_to_the_one_row(self):
        self.assertRedirects(self.client.get(URL), f"{URL}1/change/")

    def test_the_page_shows_money_in_pounds(self):
        response = self.client.get(f"{URL}1/change/")
        self.assertContains(response, "Key deposit (£)")
        self.assertContains(response, 'value="15.00"')

    def test_money_is_entered_in_pounds_and_each_change_is_logged(self):
        self.assertEqual(self.post(minimum_subscription_pounds="30", key_deposit_pounds="17.50").status_code, 302)
        row = SiteSettings.load()
        self.assertEqual((row.minimum_subscription_pence, row.key_deposit_pence), (3000, 1750))
        changes = {c.setting: (c.old_value, c.new_value, c.changed_by) for c in SettingChange.objects.all()}
        self.assertEqual(changes["minimum monthly subscription"], ("£25.00", "£30.00", self.admin))
        self.assertEqual(changes["key deposit"], ("£15.00", "£17.50", self.admin))
        self.assertEqual(len(changes), 2)

    def test_saving_without_a_change_logs_nothing(self):
        self.post()
        self.assertFalse(SettingChange.objects.exists())

    def test_values_are_checked(self):
        response = self.post(minimum_subscription_pounds="25.50", reader_check_in_seconds="5", key_deposit_pounds="0")
        self.assertEqual(response.status_code, 200)
        errors = response.context["adminform"].form.errors
        self.assertIn("Enter whole pounds.", str(errors["minimum_subscription_pounds"]))
        self.assertIn("reader_check_in_seconds", errors)
        self.assertIn("key_deposit_pounds", errors)
        self.assertEqual(SiteSettings.load().minimum_subscription_pence, 2500)

    def test_cannot_be_added_or_deleted(self):
        SiteSettings.load()
        self.assertEqual(self.client.get(f"{URL}add/").status_code, 403)
        self.assertEqual(self.client.get(f"{URL}1/delete/").status_code, 403)

    def test_only_admins_can_change_it(self):
        someone = Member.objects.create_user("kim@example.com", "a-good-password", given_name="K", family_name="M")
        self.client.force_login(someone)
        self.post(minimum_subscription_pounds="1")
        self.assertEqual(SiteSettings.load().minimum_subscription_pence, 2500)


class SettingsInUseTests(TestCase):
    def test_space_name_is_on_every_page(self):
        change(space_name="Hove Makers")
        self.assertContains(self.client.get("/"), "Hove Makers")

    def test_key_deposit_and_box_fee_are_charged_at_the_current_price(self):
        who = member()
        change(key_deposit_pence=2000, box_fee_pence=1200)
        self.assertEqual(services.pay_key_deposit(who, Payment.Source.CASH).amount_pence, 2000)
        self.assertEqual(services.pay_box_fee(who, Payment.Source.CASH).amount_pence, 1200)

    def test_minimum_session_charge(self):
        machine = laser()  # £3 an hour
        session = UsageSession(equipment=machine, member=member(), started_at=timezone.now())
        session.ended_at = session.started_at + timedelta(minutes=5)
        self.assertEqual(rules.session_fee_pence(machine, session), 75)  # 15 minutes
        change(minimum_session_minutes=30)
        self.assertEqual(rules.session_fee_pence(machine, session), 150)

    def test_changing_the_minimum_charge_tells_machine_readers(self):
        machine = laser()
        SiteSettings.load()
        change(minimum_session_minutes=20)
        machine.refresh_from_db()
        self.assertEqual(machine.config_version, 2)

    def test_credit_allowed_below_zero_to_start_a_session(self):
        machine = laser()
        who = member()
        trained(who, machine)
        services.record_payment(who, Payment.Reason.EQUIPMENT_FEE, Payment.Source.BALANCE, 400)  # credit −£4
        self.assertEqual(rules.machine_refusal_reason(who, machine), "")
        change(machine_credit_limit_pence=300)
        self.assertEqual(rules.machine_refusal_reason(who, machine), "insufficient_credit")

    def test_reader_timings_are_sent_at_check_in(self):
        change(reader_check_in_seconds=120, session_heartbeat_seconds=30)
        reader = Device(name="Door", device_id="door", role=Device.Role.ENTRY)
        key = reader.generate_api_key()
        reader.save()
        body = self.client.post(
            "/api/devices/v2/check-in",
            {"event": "boot"},
            content_type="application/json",
            headers={"Authorization": f"Bearer {key}"},
        ).json()
        self.assertEqual((body["heartbeat_seconds"], body["session_heartbeat_seconds"]), (120, 30))

    def test_unchecked_id_documents_are_kept_for_the_set_time(self):
        who = member()
        document = IdDocument.objects.create(
            member=who, kind="photo_id", filename="p.jpg", content_type="image/jpeg", size=3, data=b"\xff\xd8\xff"
        )
        IdDocument.objects.filter(pk=document.pk).update(uploaded_at=timezone.now() - timedelta(days=10))
        self.assertEqual(id_documents.shred_stale(), 0)
        change(id_document_max_age_days=7)
        self.assertEqual(id_documents.shred_stale(), 1)
