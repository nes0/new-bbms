"""Machine fees taken from credit, and corrected when a session changes (spec 05 §5.4, §5.6)."""

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.access import tags
from apps.access.models import Device, KeyFob
from apps.equipment import rules
from apps.equipment.models import UsageSession
from apps.equipment.tests.helpers import laser, member, trained
from apps.members.models import Member
from apps.payments import services
from apps.payments.credit import credit_pence
from apps.payments.models import Payment


def session(equipment, who, minutes, **fields):
    start = timezone.now() - timedelta(minutes=minutes + 5)
    return UsageSession.objects.create(equipment=equipment, member=who, started_at=start, **fields)


class ChargingTests(TestCase):
    def setUp(self):
        self.laser = laser(usage_cost_pence_per_hour=300)  # £3/hour
        self.who = member()

    def test_ending_a_session_charges_credit(self):
        s = session(self.laser, self.who, 60)
        rules.end_session(s, when=s.started_at + timedelta(hours=1), ended_by=UsageSession.EndedBy.TAP)
        self.assertEqual(credit_pence(self.who), -300)
        payment = s.payments.get()
        self.assertEqual(
            (payment.reason, payment.source, payment.reference), ("equipment-fee", "balance", f"{s.pk}:laser")
        )
        self.assertIsNotNone(s.charged_at)

    def test_live_balance_doesnt_count_the_fee_twice_once_charged(self):
        s = session(self.laser, self.who, 60)
        rules.end_session(s, when=s.started_at + timedelta(hours=1), ended_by=UsageSession.EndedBy.TAP)
        self.assertEqual(rules.live_balance_pence(self.who, s), -300)

    def test_free_and_short_sessions_cost_nothing(self):
        free = session(self.laser, self.who, 60, reason=UsageSession.Reason.TRAINING)
        rules.end_session(free, when=free.started_at + timedelta(hours=1), ended_by=UsageSession.EndedBy.TAP)
        short = session(self.laser, self.who, 1)
        rules.end_session(short, when=short.started_at + timedelta(seconds=40), ended_by=UsageSession.EndedBy.TAP)
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(credit_pence(self.who), 0)
        self.assertIsNotNone(free.charged_at)

    def test_settling_twice_changes_nothing(self):
        s = session(self.laser, self.who, 60)
        rules.end_session(s, when=s.started_at + timedelta(hours=1), ended_by=UsageSession.EndedBy.TAP)
        self.assertIsNone(services.settle_session(s))
        self.assertEqual(Payment.objects.count(), 1)

    def test_a_reclassified_session_is_refunded_with_a_new_row(self):
        s = session(self.laser, self.who, 60)
        rules.end_session(s, when=s.started_at + timedelta(hours=1), ended_by=UsageSession.EndedBy.TAP)
        s.reason = UsageSession.Reason.TESTING
        s.fee_pence = rules.session_fee_pence(self.laser, s)
        s.save()
        refund = services.settle_session(s)
        original = s.payments.order_by("pk").first()
        self.assertEqual((refund.amount_pence, refund.corrects), (-300, original))
        self.assertEqual(original.amount_pence, 300)  # never edited
        self.assertEqual(credit_pence(self.who), 0)

    def test_sessions_settled_by_the_legacy_system_are_left_alone(self):
        s = session(self.laser, self.who, 60, legacy_id=77, charged_at=timezone.now())
        s.ended_at = s.started_at + timedelta(hours=2)
        s.fee_pence = 600
        s.save()
        self.assertIsNone(services.settle_session(s))
        self.assertFalse(Payment.objects.exists())

    def test_starting_needs_credit_above_minus_five_pounds(self):
        trained(self.who, self.laser)
        Payment.objects.create(member=self.who, reason="equipment-fee", source="balance", amount_pence=500)
        self.assertEqual(rules.machine_refusal_reason(self.who, self.laser), "insufficient_credit")
        services.record_payment(self.who, Payment.Reason.BALANCE, Payment.Source.CASH, 1)
        self.assertEqual(rules.machine_refusal_reason(self.who, self.laser), "")


class MergeTests(TestCase):
    def test_tapping_straight_back_in_pays_one_minimum_charge(self):
        equipment, who = laser(usage_cost_pence_per_hour=400), member()
        now = timezone.now()
        a = UsageSession.objects.create(equipment=equipment, member=who, started_at=now - timedelta(minutes=20))
        rules.end_session(a, when=now - timedelta(minutes=15), ended_by=UsageSession.EndedBy.TAP)
        b = UsageSession.objects.create(
            equipment=equipment, member=who, started_at=now - timedelta(minutes=14, seconds=30)
        )
        rules.end_session(b, when=now - timedelta(minutes=10), ended_by=UsageSession.EndedBy.TAP)
        self.assertEqual(credit_pence(who), -200)  # two 15-minute minimum charges of £1
        self.assertEqual(rules.tidy_up(now)["merged"], 1)
        a.refresh_from_db()
        self.assertEqual(a.fee_pence, 100)  # one 10-minute session: the 15-minute minimum
        self.assertEqual(credit_pence(who), -100)
        self.assertEqual(a.payments.count(), 3)  # both charges, and the refund


class AdminAdjustmentTests(TestCase):
    def setUp(self):
        admin = Member.objects.create_superuser("admin@example.com", "a-good-password", given_name="A", family_name="D")
        self.client.force_login(admin)
        self.laser = laser(usage_cost_pence_per_hour=300)
        self.who = member()
        self.start = (timezone.now() - timedelta(hours=6)).replace(microsecond=0)
        self.session = UsageSession.objects.create(equipment=self.laser, member=self.who, started_at=self.start)
        # Forgot to tap out; closed four hours later and charged £12.
        rules.end_session(self.session, when=self.start + timedelta(hours=4), ended_by=UsageSession.EndedBy.TIDY_UP)

    def test_shortening_a_session_refunds_the_difference(self):
        local = timezone.localtime
        end = self.start + timedelta(hours=1)
        response = self.client.post(
            reverse("admin:equipment_usagesession_change", args=[self.session.pk]),
            {
                "equipment": self.laser.pk,
                "member": self.who.pk,
                "started_at_0": local(self.start).strftime("%d/%m/%Y"),
                "started_at_1": local(self.start).strftime("%H:%M:%S"),
                "last_heartbeat_at_0": "",
                "last_heartbeat_at_1": "",
                "ended_at_0": local(end).strftime("%d/%m/%Y"),
                "ended_at_1": local(end).strftime("%H:%M:%S"),
                "ended_by": UsageSession.EndedBy.TIDY_UP,
                "reason": "",
                "adjusted_reason": "Forgot to tap out",
            },
            follow=True,
        )
        self.assertContains(response, "The member was refunded £9.00.")
        self.assertEqual(credit_pence(self.who), -300)
        self.assertEqual(
            list(self.session.payments.values_list("amount_pence", flat=True).order_by("pk")), [1200, -900]
        )


class DeviceBalanceTests(TestCase):
    def test_v1_sends_the_real_balance(self):
        equipment, who = laser(), member()
        trained(who, equipment)
        services.record_payment(who, Payment.Reason.BALANCE, Payment.Source.CASH, 1234)
        device = Device(name="Laser", device_id="laser", role=Device.Role.EQUIPMENT)
        key = device.generate_api_key()
        device.save()
        fob = KeyFob.objects.create(member=who, tag_id=tags.full_tag("55", 4242))
        response = self.client.get(f"/acs/status/{fob.tag_id}", headers={"ApiKey": key})
        self.assertEqual(response.json()["user"]["cash_balance"], 1234)
