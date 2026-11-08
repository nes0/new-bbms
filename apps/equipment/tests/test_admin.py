"""Adjusting a session's times in the admin (spec 05 §5.6)."""

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.equipment.models import UsageSession
from apps.members.models import Member
from apps.notifications.models import Notification
from apps.payments import services

from .helpers import laser, member


class SessionAdjustmentTests(TestCase):
    def setUp(self):
        self.admin = Member.objects.create_superuser(
            "admin@example.com", "a-good-password", given_name="A", family_name="D"
        )
        self.client.force_login(self.admin)
        self.laser = laser(usage_cost_pence_per_hour=300)
        self.start = (timezone.now() - timedelta(hours=5)).replace(microsecond=0)
        # Forgot to tap out: the tidy-up closed it 4 hours later.
        self.session = UsageSession.objects.create(
            equipment=self.laser,
            member=member(),
            started_at=self.start,
            ended_at=self.start + timedelta(hours=4),
            ended_by=UsageSession.EndedBy.TIDY_UP,
            fee_pence=1200,
        )
        self.url = reverse("admin:equipment_usagesession_change", args=[self.session.pk])

    def post(self, ended_at, reason="Forgot to tap out; actually used for an hour"):
        local = timezone.localtime
        data = {
            "equipment": self.laser.pk,
            "member": self.session.member.pk,
            "started_at_0": local(self.start).strftime("%d/%m/%Y"),
            "started_at_1": local(self.start).strftime("%H:%M:%S"),
            "last_heartbeat_at_0": "",
            "last_heartbeat_at_1": "",
            "ended_at_0": local(ended_at).strftime("%d/%m/%Y"),
            "ended_at_1": local(ended_at).strftime("%H:%M:%S"),
            "ended_by": UsageSession.EndedBy.TIDY_UP,
            "reason": "",
            "adjusted_reason": reason,
        }
        return self.client.post(self.url, data)

    def test_adjusting_times_recalculates_the_fee_and_keeps_the_original(self):
        response = self.post(self.start + timedelta(hours=1))
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and response.context["errors"])
        self.session.refresh_from_db()
        self.assertEqual(self.session.fee_pence, 300)
        self.assertEqual(self.session.original_ended_at, self.start + timedelta(hours=4))
        self.assertEqual(self.session.adjusted_by, self.admin)

    def test_the_member_is_told_the_old_and_new_times_and_the_refund(self):
        services.settle_session(self.session)  # the original £12 charge
        self.post(self.start + timedelta(hours=1))
        notification = Notification.objects.get(member=self.session.member, kind="session_adjusted")
        self.assertIn("Red laser", notification.message)
        self.assertIn("now it's", notification.message)
        self.assertIn("refunded £9.00", notification.message)

    def test_nobody_is_told_if_the_times_didnt_change(self):
        self.post(self.start + timedelta(hours=4))
        self.assertFalse(Notification.objects.exists())

    def test_a_reason_is_required(self):
        response = self.post(self.start + timedelta(hours=1), reason="")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Give a reason")

    def test_end_must_be_after_start(self):
        response = self.post(self.start - timedelta(minutes=5))
        self.assertContains(response, "The end must be after the start")
