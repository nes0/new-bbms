"""Sign-off and course completions in the admin (spec 05 §5.8, 06 §6.12), the 15-minute job and the deploy checks."""

from datetime import timedelta
from io import StringIO

from django.core import checks
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.equipment.admin import InductionForm
from apps.equipment.models import Induction, TrainingCompletion
from apps.members.models import Member
from apps.moodle import client
from apps.moodle.checks import moodle_is_set_up
from apps.notifications.models import Notification

from .helpers import COURSE, course_machine, member, set_moodle_address, waiting


def stamp(moment):
    return timezone.localtime(moment).strftime("%Y-%m-%d %H:%M:%S")


def form_for(induction, trained_at, **extra):
    return InductionForm(
        data={
            "member": induction.member_id,
            "category": induction.category,
            "paid": "on",
            "trained_at": stamp(trained_at),
            "created_at": "2026-01-01 10:00:00",
            **extra,
        },
        instance=induction,
    )


class SignOffNeedsTheCourseTests(TestCase):
    def setUp(self):
        self.machine = course_machine()
        self.kim = member()
        self.induction = waiting(self.kim, self.machine)

    def test_cant_sign_off_before_the_course_is_finished(self):
        form = form_for(self.induction, timezone.now())
        self.assertFalse(form.is_valid())
        self.assertIn("hasn't finished the online course for Red laser", " ".join(form.non_field_errors()))

    def test_can_sign_off_once_it_is_recorded(self):
        TrainingCompletion.objects.create(member=self.kim, moodle_course_id=COURSE, completed_at=timezone.now())
        self.assertTrue(form_for(self.induction, timezone.now()).is_valid())

    def test_a_cleared_completion_doesnt_count(self):
        TrainingCompletion.objects.create(
            member=self.kim, moodle_course_id=COURSE, completed_at=timezone.now(), cleared_at=timezone.now()
        )
        self.assertFalse(form_for(self.induction, timezone.now()).is_valid())

    def test_a_machine_without_a_course_is_unaffected(self):
        plain = course_machine(name="Drill", slug="drill", induction_category="drill", moodle_course_id=None)
        induction = waiting(self.kim, plain)
        self.assertTrue(form_for(induction, timezone.now()).is_valid())

    def test_editing_something_else_on_someone_already_signed_off_isnt_blocked(self):
        Induction.objects.filter(pk=self.induction.pk).update(trained_at=timezone.now() - timedelta(days=500))
        self.induction.refresh_from_db()
        form = form_for(self.induction, self.induction.trained_at, is_trainer="on")
        self.assertTrue(form.is_valid(), form.errors)


class AdminListTests(TestCase):
    def setUp(self):
        self.admin = Member.objects.create_superuser(
            "admin@example.com", "a-good-password", given_name="A", family_name="D"
        )
        self.client.force_login(self.admin)
        self.machine = course_machine()
        self.fake = client.FakeMoodle()
        use = client.use(self.fake)
        use.__enter__()
        self.addCleanup(use.__exit__, None, None, None)

    def test_opening_the_waiting_list_checks_moodle_and_shows_course_status(self):
        done, not_done = member(), member()
        for who in (done, not_done):
            waiting(who, self.machine)
            self.fake.add_user(who, 100 + who.pk)
        self.fake.complete(done, COURSE)
        response = self.client.get(reverse("admin:equipment_induction_changelist") + "?waiting=yes")
        self.assertContains(response, "Finished")
        self.assertContains(response, "Not finished")
        self.assertTrue(TrainingCompletion.objects.filter(member=done).exists())

    def test_the_normal_list_doesnt_call_moodle(self):
        waiting(member(), self.machine)
        self.client.get(reverse("admin:equipment_induction_changelist"))
        self.assertEqual(self.fake.calls, [])

    def test_a_moodle_outage_warns_but_the_list_still_opens(self):
        waiting(member(), self.machine)
        self.fake.down = True
        response = self.client.get(reverse("admin:equipment_induction_changelist") + "?waiting=yes")
        self.assertContains(response, "Moodle couldn")
        self.assertEqual(response.status_code, 200)

    def test_clearing_a_completion(self):
        kim = member()
        record = TrainingCompletion.objects.create(member=kim, moodle_course_id=COURSE, completed_at=timezone.now())
        self.client.post(
            reverse("admin:equipment_trainingcompletion_changelist"),
            {"action": "clear", "_selected_action": [record.pk]},
        )
        record.refresh_from_db()
        self.assertIsNotNone(record.cleared_at)
        self.assertEqual(record.cleared_by, self.admin)


class JobTests(TestCase):
    def run_job(self):
        out = StringIO()
        call_command("check_moodle_completions", stdout=out)
        return out.getvalue()

    def test_the_job_records_whoever_has_finished(self):
        machine = course_machine()
        kim = member()
        waiting(kim, machine)
        fake = client.FakeMoodle()
        fake.add_user(kim, 5)
        fake.complete(kim, COURSE)
        with client.use(fake):
            self.assertIn("Checked 1 member(s); 1 finished their course.", self.run_job())
        self.assertTrue(TrainingCompletion.objects.filter(member=kim).exists())
        self.assertTrue(Notification.objects.filter(member=kim, kind="course_completed").exists())

    def test_the_job_says_when_moodle_cant_be_reached(self):
        machine = course_machine()
        waiting(member(), machine)
        fake = client.FakeMoodle()
        fake.down = True
        with client.use(fake):
            self.assertIn("Moodle couldn't be reached.", self.run_job())

    def test_the_job_does_nothing_when_moodle_isnt_set_up(self):
        from unittest import mock

        with mock.patch.object(client, "enabled", return_value=False):
            self.assertIn("nothing to check", self.run_job())


class DeployCheckTests(TestCase):
    def ids(self):
        return sorted(message.id for message in moodle_is_set_up(None))

    @override_settings(OIDC_RSA_PRIVATE_KEY="", MOODLE_TOKEN="")
    def test_nothing_set_up(self):
        self.assertEqual(self.ids(), ["moodle.W001", "moodle.W002", "moodle.W003"])

    @override_settings(OIDC_RSA_PRIVATE_KEY="a key", MOODLE_TOKEN="a token")
    def test_all_set_up(self):
        set_moodle_address()
        self.assertEqual(self.ids(), [])

    def test_the_checks_are_registered_for_deploy(self):
        found = checks.run_checks(include_deployment_checks=True, tags=[checks.Tags.security])
        self.assertTrue({"moodle.W001", "moodle.W002", "moodle.W003"} & {message.id for message in found})
