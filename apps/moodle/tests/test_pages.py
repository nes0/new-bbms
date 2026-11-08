from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from apps.equipment.models import Induction, TrainingCompletion
from apps.equipment.tests.helpers import trained
from apps.moodle import client

from .helpers import COURSE, course_machine, member, set_moodle_address


class TrainingPageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.kim = member()
        self.machine = course_machine()
        self.fake = client.FakeMoodle()
        use = client.use(self.fake)
        use.__enter__()
        self.addCleanup(use.__exit__, None, None, None)
        self.client.force_login(self.kim)
        self.url = reverse("moodle:training")

    def test_you_must_log_in(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.client.post(reverse("moodle:check-now", args=[COURSE])).status_code, 302)

    def test_the_page_lists_the_course_with_its_machines_and_a_link(self):
        set_moodle_address()
        response = self.client.get(self.url)
        self.assertContains(response, "Red laser")
        self.assertContains(response, "https://moodle.example.com/course/view.php?id=7")
        self.assertContains(response, "finished the course, check now")
        self.assertContains(response, "You haven't finished this course yet.")

    def test_without_a_moodle_address_there_is_no_link(self):
        self.assertNotContains(self.client.get(self.url), "course/view.php")

    def test_a_finished_course_shows_when(self):
        self.fake.add_user(self.kim)
        self.fake.complete(self.kim, COURSE)
        TrainingCompletion.objects.create(member=self.kim, moodle_course_id=COURSE, completed_at=timezone.now())
        response = self.client.get(self.url)
        self.assertContains(response, "You finished the course on")
        self.assertContains(response, "A trainer can sign you off now.")
        self.assertNotContains(response, "finished the course, check now")  # no button once it's done

    def test_someone_signed_off_before_the_course_keeps_access_and_is_told(self):
        trained(self.kim, self.machine, when=timezone.now() - timedelta(days=400))
        self.assertContains(self.client.get(self.url), "you keep your access")

    def test_no_courses(self):
        self.machine.delete()
        self.assertContains(self.client.get(self.url), "None of our machines need an online course")

    def test_the_account_page_links_here(self):
        self.assertContains(self.client.get("/account/"), self.url)


class CheckNowTests(TestCase):
    def setUp(self):
        cache.clear()
        self.kim = member()
        course_machine()
        self.fake = client.FakeMoodle()
        use = client.use(self.fake)
        use.__enter__()
        self.addCleanup(use.__exit__, None, None, None)
        self.client.force_login(self.kim)
        self.url = reverse("moodle:check-now", args=[COURSE])

    def press(self):
        return self.client.post(self.url, follow=True)

    def says(self, response, text):
        self.assertContains(response, escape(text))

    def test_finished(self):
        self.fake.add_user(self.kim)
        self.fake.complete(self.kim, COURSE)
        self.says(self.press(), "we've recorded that you've finished the course")
        self.assertTrue(TrainingCompletion.objects.filter(member=self.kim).exists())

    def test_not_finished(self):
        self.fake.add_user(self.kim)
        self.says(self.press(), "doesn't show the course as finished yet")
        self.assertFalse(TrainingCompletion.objects.exists())

    def test_moodle_doesnt_know_them(self):
        self.says(self.press(), "Log in with BBMS")

    def test_moodle_down(self):
        self.fake.down = True
        self.says(self.press(), "couldn't reach Moodle")

    def test_pressing_again_straight_away_waits(self):
        self.fake.add_user(self.kim)
        self.press()
        self.fake.calls.clear()
        self.says(self.press(), "Please wait half a minute")
        self.assertEqual(self.fake.calls, [])

    def test_get_isnt_allowed_and_unknown_courses_are_refused(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        response = self.client.post(reverse("moodle:check-now", args=[999]), follow=True)
        self.assertContains(response, escape("isn't one of our online courses"))

    @override_settings(MOODLE_TOKEN="")
    def test_when_moodle_cant_be_checked_at_all(self):
        from unittest import mock

        with client.use(client.NotConfigured()), mock.patch.object(client, "enabled", return_value=False):
            self.says(self.press(), "We can't check online courses at the moment")


class SignedOffRowTests(TestCase):
    def test_signed_off_members_see_it(self):
        kim = member()
        machine = course_machine()
        trained(kim, machine)
        TrainingCompletion.objects.create(member=kim, moodle_course_id=COURSE, completed_at=timezone.now())
        self.client.force_login(kim)
        self.assertContains(self.client.get(reverse("moodle:training")), "You're signed off")
        self.assertTrue(Induction.objects.filter(member=kim).exists())
