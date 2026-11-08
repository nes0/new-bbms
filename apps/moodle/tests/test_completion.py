from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.equipment import rules
from apps.equipment.models import Equipment, TrainingCompletion
from apps.moodle import client, completion
from apps.moodle.models import MoodleAccount
from apps.notifications.models import Notification

from .helpers import COURSE, course_machine, member, waiting


class CheckTests(TestCase):
    def setUp(self):
        self.fake = client.FakeMoodle()
        self.kim = member()
        self.machine = course_machine()
        self.use = client.use(self.fake)
        self.use.__enter__()
        self.addCleanup(self.use.__exit__, None, None, None)

    def test_a_member_moodle_doesnt_know_yet(self):
        self.assertEqual(completion.check(self.kim, COURSE), completion.NO_ACCOUNT)
        self.assertFalse(TrainingCompletion.objects.exists())

    def test_not_finished_yet_remembers_who_they_are_in_moodle(self):
        self.fake.add_user(self.kim, 55)
        self.assertEqual(completion.check(self.kim, COURSE), completion.NOT_YET)
        self.assertEqual(MoodleAccount.objects.get(member=self.kim).moodle_user_id, 55)
        self.fake.calls.clear()
        completion.check(self.kim, COURSE)
        self.assertNotIn(("find_user_id", self.kim.pk), self.fake.calls)  # found once, then remembered

    def test_finished_is_recorded_with_when_and_the_member_is_told(self):
        self.fake.add_user(self.kim, 55)
        finished = timezone.now() - timedelta(days=2)
        self.fake.complete(self.kim, COURSE, finished)
        self.assertEqual(completion.check(self.kim, COURSE), completion.COMPLETED)
        record = TrainingCompletion.objects.get()
        self.assertEqual((record.member, record.moodle_course_id, record.completed_at), (self.kim, COURSE, finished))
        self.assertTrue(completion.has_completed(self.kim, COURSE))
        (notice,) = Notification.objects.filter(member=self.kim, kind="course_completed")
        self.assertIn("Red laser", notice.message)

    def test_a_recorded_completion_is_not_asked_about_again(self):
        self.fake.add_user(self.kim, 55)
        self.fake.complete(self.kim, COURSE)
        completion.check(self.kim, COURSE)
        self.fake.calls.clear()
        self.assertEqual(completion.check(self.kim, COURSE), completion.ALREADY)
        self.assertEqual(self.fake.calls, [])

    def test_moodle_being_down_records_nothing(self):
        self.fake.add_user(self.kim, 55)
        self.fake.down = True
        self.assertEqual(completion.check(self.kim, COURSE), completion.UNAVAILABLE)
        self.assertFalse(TrainingCompletion.objects.exists())

    def test_a_cleared_completion_stays_cleared_until_they_finish_again(self):
        self.fake.add_user(self.kim, 55)
        old = timezone.now() - timedelta(days=30)
        self.fake.complete(self.kim, COURSE, old)
        completion.check(self.kim, COURSE)
        record = TrainingCompletion.objects.get()
        record.cleared_at = timezone.now() - timedelta(days=1)
        record.save()
        self.assertFalse(completion.has_completed(self.kim, COURSE))
        self.assertEqual(completion.check(self.kim, COURSE), completion.NOT_YET)  # Moodle still shows the old one
        self.assertFalse(completion.has_completed(self.kim, COURSE))
        self.fake.complete(self.kim, COURSE, timezone.now())  # retaken
        self.assertEqual(completion.check(self.kim, COURSE), completion.COMPLETED)
        record.refresh_from_db()
        self.assertEqual((record.cleared_at, completion.has_completed(self.kim, COURSE)), (None, True))
        self.assertEqual(TrainingCompletion.objects.count(), 1)

    def test_the_machine_rule_uses_only_the_stored_record(self):
        from apps.equipment.tests.helpers import trained

        trained(self.kim, self.machine)
        self.fake.add_user(self.kim, 55)
        self.fake.complete(self.kim, COURSE)
        self.assertEqual(rules.machine_refusal_reason(self.kim, self.machine), "course_incomplete")  # not asked
        self.assertEqual(self.fake.calls, [])
        completion.check(self.kim, COURSE)
        self.assertEqual(rules.machine_refusal_reason(self.kim, self.machine), "")
        TrainingCompletion.objects.update(cleared_at=timezone.now())
        self.assertEqual(rules.machine_refusal_reason(self.kim, self.machine), "course_incomplete")


class CheckWaitingTests(TestCase):
    def setUp(self):
        self.fake = client.FakeMoodle()
        self.machine = course_machine()
        use = client.use(self.fake)
        use.__enter__()
        self.addCleanup(use.__exit__, None, None, None)

    def test_only_members_waiting_for_sign_off_on_a_machine_with_a_course_are_checked(self):
        waiting_one, waiting_two = member(), member()
        for who in (waiting_one, waiting_two):
            waiting(who, self.machine)
            self.fake.add_user(who, 100 + who.pk)
        self.fake.complete(waiting_one, COURSE)
        unpaid, signed_off, other_machine = member(), member(), member()
        waiting(unpaid, self.machine, paid=False)
        waiting(signed_off, self.machine).__class__.objects.filter(member=signed_off).update(trained_at=timezone.now())

        plain = Equipment.objects.create(
            name="Drill", slug="drill", requires_induction=True, induction_category="drill"
        )
        waiting(other_machine, plain)
        summary = completion.check_waiting()
        self.assertEqual((summary.checked, summary.completed, summary.unavailable), (2, 1, False))

    def test_a_machine_without_a_course_is_ignored_and_a_member_is_checked_once_per_course(self):
        second = course_machine(name="Blue laser", slug="laser2")  # shares the induction category and course
        who = member()
        waiting(who, self.machine)
        self.fake.add_user(who, 77)
        self.assertEqual(completion.check_waiting().checked, 1)
        self.assertEqual(second.moodle_course_id, COURSE)

    def test_an_outage_stops_after_the_first_failure(self):
        for _ in range(3):
            who = member()
            waiting(who, self.machine)
            self.fake.add_user(who, 100 + who.pk)
        self.fake.down = True
        summary = completion.check_waiting()
        self.assertEqual((summary.checked, summary.unavailable), (0, True))
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "find_user_id"]), 1)

    def test_a_limit(self):
        for _ in range(4):
            who = member()
            waiting(who, self.machine)
            self.fake.add_user(who, 100 + who.pk)
        self.assertEqual(completion.check_waiting(limit=2).checked, 2)
