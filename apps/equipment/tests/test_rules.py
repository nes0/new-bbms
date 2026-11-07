from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.equipment import rules
from apps.equipment.models import Equipment, Induction, TrainingCompletion, UsageSession
from apps.members.models import Member

from .helpers import laser, member, trained


class MachineAccessTests(TestCase):
    def setUp(self):
        self.laser = laser()

    def test_trained_member_may_use_it(self):
        who = member()
        trained(who, self.laser)
        self.assertEqual(rules.machine_refusal_reason(who, self.laser), "")

    def test_untrained_paid_or_unpaid_is_refused(self):
        who = member()
        self.assertEqual(rules.machine_refusal_reason(who, self.laser), "not_trained")
        Induction.objects.create(member=who, category="laser", paid=True)  # paid, waiting for training
        self.assertEqual(rules.machine_refusal_reason(who, self.laser), "not_trained")

    def test_machines_without_induction_are_open_to_members(self):
        drill = Equipment.objects.create(name="Pillar drill", slug="pillar-drill")
        self.assertEqual(rules.machine_refusal_reason(member(), drill), "")

    def test_shared_induction_category(self):
        drill2 = Equipment.objects.create(
            name="Drill 2", slug="drill-2", requires_induction=True, induction_category="drill"
        )
        drill1 = Equipment.objects.create(
            name="Drill 1", slug="drill-1", requires_induction=True, induction_category="drill"
        )
        who = member()
        trained(who, drill1)
        self.assertEqual(rules.machine_refusal_reason(who, drill2), "")

    def test_non_members_are_refused(self):
        who = member(status=Member.Status.LEFT)
        trained(who, self.laser)
        self.assertEqual(rules.machine_refusal_reason(who, self.laser), "not_member")

    def test_out_of_order_only_for_its_team(self):
        self.laser.status = Equipment.Status.OUT_OF_ORDER
        self.laser.save()
        ordinary, on_team = member(), member()
        trained(ordinary, self.laser)
        trained(on_team, self.laser)
        on_team.groups.add(self.laser.team)
        self.assertEqual(rules.machine_refusal_reason(ordinary, self.laser), "out_of_order")
        self.assertEqual(rules.machine_refusal_reason(on_team, self.laser), "")
        session = rules.start_session(self.laser, on_team, close_existing=False)
        self.assertEqual(session.reason, UsageSession.Reason.MAINTENANCE)

    def test_credit_must_be_above_minus_five_pounds(self):
        who = member()
        trained(who, self.laser)
        with mock.patch.object(rules, "member_credit_pence", return_value=-500):
            self.assertEqual(rules.machine_refusal_reason(who, self.laser), "insufficient_credit")
        with mock.patch.object(rules, "member_credit_pence", return_value=-499):
            self.assertEqual(rules.machine_refusal_reason(who, self.laser), "")


class MoodleCourseTests(TestCase):
    def setUp(self):
        self.course_since = timezone.now() - timedelta(days=30)
        self.laser = laser(moodle_course_id=42, moodle_course_since=self.course_since)

    def test_course_needed_for_members_trained_after_it_was_added(self):
        who = member()
        trained(who, self.laser)
        self.assertEqual(rules.machine_refusal_reason(who, self.laser), "course_incomplete")
        TrainingCompletion.objects.create(member=who, moodle_course_id=42, completed_at=timezone.now())
        self.assertEqual(rules.machine_refusal_reason(who, self.laser), "")

    def test_members_trained_before_the_course_keep_access(self):
        who = member()
        trained(who, self.laser, when=self.course_since - timedelta(days=1))
        self.assertEqual(rules.machine_refusal_reason(who, self.laser), "")

    def test_unless_the_course_is_required_for_everyone(self):
        self.laser.course_required_for_existing = True
        self.laser.save()
        who = member()
        trained(who, self.laser, when=self.course_since - timedelta(days=1))
        self.assertEqual(rules.machine_refusal_reason(who, self.laser), "course_incomplete")


class FeeTests(TestCase):
    def setUp(self):
        self.laser = laser(usage_cost_pence_per_hour=300)  # £3/hour
        self.who = member()

    def fee_for(self, minutes, reason=UsageSession.Reason.NORMAL):
        start = timezone.now() - timedelta(minutes=minutes)
        session = UsageSession.objects.create(equipment=self.laser, member=self.who, started_at=start, reason=reason)
        rules.end_session(session, ended_by=UsageSession.EndedBy.TAP)
        return session.fee_pence

    def test_minimum_charge_is_15_minutes(self):
        self.assertEqual(self.fee_for(5), 75)  # 15 min of £3/hour

    def test_charged_by_session_time(self):
        self.assertEqual(self.fee_for(60), 300)
        self.assertEqual(self.fee_for(90), 450)

    def test_rounded_to_the_penny(self):
        laser2 = laser(slug="laser-2", usage_cost_pence_per_hour=250)
        start = timezone.now() - timedelta(minutes=17)
        session = UsageSession.objects.create(equipment=laser2, member=self.who, started_at=start)
        rules.end_session(session, ended_by=UsageSession.EndedBy.TAP)
        self.assertEqual(session.fee_pence, 71)  # 250 * 17/60 = 70.83

    def test_training_testing_and_maintenance_are_free(self):
        for reason in rules.UNCHARGED_REASONS:
            self.assertEqual(self.fee_for(60, reason), 0, reason)

    def test_sessions_of_a_minute_or_less_are_removed_and_free(self):
        start = timezone.now() - timedelta(seconds=50)
        session = UsageSession.objects.create(equipment=self.laser, member=self.who, started_at=start)
        rules.end_session(session, ended_by=UsageSession.EndedBy.TAP)
        self.assertTrue(session.removed)
        self.assertEqual(session.fee_pence, 0)


class SessionTests(TestCase):
    def setUp(self):
        self.laser = laser()

    def test_v1_start_closes_any_open_session(self):
        first, second = member(), member()
        old = rules.start_session(self.laser, first, close_existing=True)
        new = rules.start_session(self.laser, second, close_existing=True)
        old.refresh_from_db()
        self.assertIsNotNone(old.ended_at)
        self.assertEqual(rules.open_session(self.laser), new)


class TidyUpTests(TestCase):
    def setUp(self):
        self.laser = laser()
        self.who = member()
        self.now = timezone.now()

    def session(self, start_ago, end_ago=None, heartbeat_ago=None, **fields):
        return UsageSession.objects.create(
            equipment=self.laser,
            member=self.who,
            started_at=self.now - start_ago,
            ended_at=(self.now - end_ago) if end_ago is not None else None,
            last_heartbeat_at=(self.now - heartbeat_ago) if heartbeat_ago is not None else None,
            **fields,
        )

    def test_stale_session_closed_at_last_heartbeat(self):
        s = self.session(timedelta(hours=3), heartbeat_ago=timedelta(hours=2))
        rules.tidy_up(self.now)
        s.refresh_from_db()
        self.assertEqual(s.ended_at, self.now - timedelta(hours=2))
        self.assertEqual(s.ended_by, UsageSession.EndedBy.TIDY_UP)
        self.assertEqual(s.fee_pence, 300)

    def test_never_heard_from_closes_a_minute_after_start(self):
        s = self.session(timedelta(hours=3))
        rules.tidy_up(self.now)
        s.refresh_from_db()
        self.assertEqual(s.ended_at, s.started_at + timedelta(minutes=1))

    def test_live_session_left_alone(self):
        s = self.session(timedelta(hours=3), heartbeat_ago=timedelta(minutes=5))
        rules.tidy_up(self.now)
        s.refresh_from_db()
        self.assertIsNone(s.ended_at)

    def test_back_to_back_sessions_are_merged(self):
        a = self.session(timedelta(minutes=60), end_ago=timedelta(minutes=40))
        b = self.session(timedelta(minutes=40) - timedelta(seconds=30), end_ago=timedelta(minutes=10))
        counts = rules.tidy_up(self.now)
        self.assertEqual(counts["merged"], 1)
        a.refresh_from_db()
        self.assertEqual(a.ended_at, self.now - timedelta(minutes=10))
        self.assertFalse(UsageSession.objects.filter(pk=b.pk).exists())

    def test_sessions_with_a_gap_are_not_merged(self):
        self.session(timedelta(minutes=60), end_ago=timedelta(minutes=40))
        self.session(timedelta(minutes=30), end_ago=timedelta(minutes=10))
        self.assertEqual(rules.tidy_up(self.now)["merged"], 0)

    def test_tidy_up_twice_changes_nothing_more(self):
        self.session(timedelta(hours=3), heartbeat_ago=timedelta(hours=2))
        rules.tidy_up(self.now)
        self.assertEqual(rules.tidy_up(self.now), {"closed": 0, "merged": 0})
