"""The members' equipment pages (spec 05 §5.7): the list, a machine's page, faults, services and sessions."""

from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.equipment import rules
from apps.equipment.models import Equipment, Induction, MachineStatusChange, ServiceRecord, TrainingCompletion
from apps.members.models import Member
from apps.moodle import client as moodle
from apps.notifications.models import Notification
from apps.payments import services
from apps.payments.credit import credit_pence
from apps.payments.models import Payment

from .helpers import laser, member, trained


def top_up(who, pence=2000):
    services.record_payment(who, Payment.Reason.BALANCE, Payment.Source.CASH, pence)


def finished_session(machine, who, minutes=60):
    """A session of `minutes`, ended and charged."""
    session = rules.start_session(machine, who, close_existing=False)
    session.started_at = timezone.now() - timedelta(minutes=minutes)
    session.save(update_fields=["started_at"])
    return rules.end_session(session, ended_by="tap")


class PagesTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.machine = laser(induction_fee_pence=2500, room="Laser room", ppe="Goggles, Gloves")
        self.drill = Equipment.objects.create(name="Pillar drill", slug="drill")
        self.kim = member(given_name="Kim")
        self.client.force_login(self.kim)
        self.url = reverse("equipment:detail", args=["laser"])

    def join_team(self, who):
        who.groups.add(self.machine.team)


class WhoSeesThemTests(PagesTestCase):
    def test_visitors_are_asked_to_log_in(self):
        self.client.logout()
        for url in (reverse("equipment:index"), self.url):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn(reverse("login"), response["Location"])

    def test_only_members_see_them(self):
        self.kim.status = Member.Status.LEFT
        self.kim.save()
        self.assertEqual(self.client.get(reverse("equipment:index")).status_code, 404)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_archived_machines_are_hidden(self):
        self.drill.archived = True
        self.drill.save()
        self.assertNotContains(self.client.get(reverse("equipment:index")), "Pillar drill")
        self.assertEqual(self.client.get(reverse("equipment:detail", args=["drill"])).status_code, 404)

    def test_the_menu_and_account_page_link_here(self):
        trained(self.kim, self.machine)
        self.assertContains(self.client.get("/"), reverse("equipment:index"))
        self.assertContains(self.client.get("/account/"), self.url)


class ListTests(PagesTestCase):
    def test_machines_are_split_by_whether_they_need_training_and_show_where_you_are(self):
        response = self.client.get(reverse("equipment:index"))
        self.assertContains(response, "Needs training")
        self.assertContains(response, "Access fee £25.00")
        self.assertContains(response, "£3.00 an hour")
        self.assertContains(response, "No training needed")
        trained(self.kim, self.machine)
        self.assertContains(self.client.get(reverse("equipment:index")), "You're trained")

    def test_out_of_order_machines_are_marked(self):
        self.machine.status = Equipment.Status.OUT_OF_ORDER
        self.machine.save()
        self.assertContains(self.client.get(reverse("equipment:index")), "Out of order")


class MachinePageTests(PagesTestCase):
    def test_details_and_the_way_to_get_trained(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Laser room")
        self.assertContains(response, "Goggles, Gloves")
        self.assertContains(response, "pay its one-off access fee of £25.00")
        self.assertContains(response, reverse("payments:pay"))

    def test_help_text_is_markdown_without_raw_html(self):
        self.machine.help_text = "**Focus** first <script>alert(1)</script>"
        self.machine.save()
        response = self.client.get(self.url)
        self.assertContains(response, "<strong>Focus</strong>")
        self.assertNotContains(response, "<script>alert(1)</script>")

    def test_waiting_and_trained(self):
        induction = Induction.objects.create(member=self.kim, category="laser", paid=True)
        self.assertContains(self.client.get(self.url), "A trainer can now sign you off")
        induction.trained_at = timezone.now()
        induction.save()
        top_up(self.kim)
        response = self.client.get(self.url)
        self.assertContains(response, "You're trained on it")
        self.assertContains(response, "Tap your fob on its reader to start.")

    def test_a_trained_member_is_told_why_they_cant_use_it_now(self):
        trained(self.kim, self.machine)
        services.record_payment(self.kim, Payment.Reason.DONATION, Payment.Source.BALANCE, 600)  # £6 below zero
        self.assertContains(self.client.get(self.url), "You don&#x27;t have enough credit.")

    def test_trainers_trained_and_waiting_are_listed_but_not_former_members(self):
        trainer = member(given_name="Tess")
        Induction.objects.create(
            member=trainer, category="laser", paid=True, trained_at=timezone.now(), is_trainer=True
        )
        trained(member(given_name="Tom"), self.machine)
        trained(member(given_name="Gone", status=Member.Status.LEFT), self.machine)
        Induction.objects.create(member=member(given_name="Wendy"), category="laser", paid=True)
        response = self.client.get(self.url)
        for name in ("Tess", "Tom", "Wendy"):
            self.assertContains(response, name)
        self.assertNotContains(response, "Gone ")

    def test_use_history_hides_removed_sessions_and_adds_up_the_time(self):
        trained(self.kim, self.machine)
        top_up(self.kim)
        finished_session(self.machine, self.kim, minutes=90)
        short = rules.start_session(self.machine, self.kim, close_existing=False)
        rules.end_session(short, ended_by="tap")  # under a minute: removed
        response = self.client.get(self.url)
        self.assertContains(response, "Used for 90 minutes")
        self.assertEqual(response.context["sessions"].paginator.count, 1)


class OnlineCourseTests(PagesTestCase):
    def setUp(self):
        super().setUp()
        self.machine.moodle_course_id = 7
        self.machine.moodle_course_since = timezone.now() - timedelta(days=7)
        self.machine.save()
        self.fake = moodle.FakeMoodle()
        use = moodle.use(self.fake)
        use.__enter__()
        self.addCleanup(use.__exit__, None, None, None)

    def test_a_member_waiting_is_asked_to_finish_the_course_and_can_check_from_the_page(self):
        Induction.objects.create(member=self.kim, category="laser", paid=True)
        response = self.client.get(self.url)
        self.assertContains(response, "Next, finish the online course")
        self.assertContains(response, reverse("moodle:check-now", args=[7]))
        self.fake.add_user(self.kim)
        self.fake.complete(self.kim, 7)
        response = self.client.post(reverse("moodle:check-now", args=[7]), {"next": self.url}, follow=True)
        self.assertEqual(response.redirect_chain[-1][0], self.url)
        self.assertContains(response, "recorded that you&#x27;ve finished the course")
        self.assertContains(response, "You've finished it.")

    def test_check_now_never_sends_you_to_another_site(self):
        response = self.client.post(reverse("moodle:check-now", args=[7]), {"next": "https://evil.example/"})
        self.assertEqual(response["Location"], reverse("moodle:training"))

    def test_someone_signed_off_before_the_course_keeps_access(self):
        trained(self.kim, self.machine, when=timezone.now() - timedelta(days=400))
        self.assertContains(self.client.get(self.url), "you keep your access")
        self.machine.course_required_for_existing = True
        self.machine.save()
        response = self.client.get(self.url)
        self.assertContains(response, "You need to finish its online course first.")
        self.assertNotContains(response, "you keep your access")

    def test_the_training_page_links_to_the_machine(self):
        self.assertContains(self.client.get(reverse("moodle:training")), self.url)

    def test_the_training_page_no_longer_says_you_keep_access_when_the_course_is_required(self):
        trained(self.kim, self.machine, when=timezone.now() - timedelta(days=400))
        self.machine.course_required_for_existing = True
        self.machine.save()
        self.assertNotContains(self.client.get(reverse("moodle:training")), "you keep your access")

    def test_a_finished_course_counts(self):
        trained(self.kim, self.machine)
        TrainingCompletion.objects.create(member=self.kim, moodle_course_id=7, completed_at=timezone.now())
        self.assertContains(self.client.get(self.url), "You've finished it.")


class ReportingTests(PagesTestCase):
    def report(self, status, note=""):
        return self.client.post(reverse("equipment:report", args=["laser"]), {"status": status, "note": note})

    def test_a_trained_member_can_report_a_fault_and_the_team_is_told(self):
        trained(self.kim, self.machine)
        fixer = member(given_name="Fixer")
        self.join_team(fixer)
        self.report("out_of_order", "Lens cracked")
        self.machine.refresh_from_db()
        self.assertEqual((self.machine.status, self.machine.status_note), ("out_of_order", "Lens cracked"))
        change = MachineStatusChange.objects.get()
        self.assertEqual((change.source, change.reported_by), ("web", self.kim))
        note = Notification.objects.get(member=fixer)
        self.assertIn("reported Red laser out of order. Lens cracked", note.message)
        self.assertEqual(note.url, self.url)

    def test_a_fault_needs_a_note(self):
        trained(self.kim, self.machine)
        self.report("out_of_order")
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.status, "working")

    def test_members_who_cant_use_it_cant_report_on_it(self):
        self.assertNotContains(self.client.get(self.url), "Report a problem")
        self.report("out_of_order", "Broken")
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.status, "working")

    def test_only_the_team_marks_it_working_but_the_reporter_can_change_the_note(self):
        trained(self.kim, self.machine)
        self.report("out_of_order", "Lens cracked")
        self.report("working")
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.status, "out_of_order")
        self.report("out_of_order", "Lens cracked, and the bed is bent")
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.status_note, "Lens cracked, and the bed is bent")

        other = member()
        trained(other, self.machine)
        self.client.force_login(other)
        self.report("out_of_order", "Something else")
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.status_note, "Lens cracked, and the bed is bent")

        fixer = member()
        self.join_team(fixer)
        self.client.force_login(fixer)
        self.report("working", "New lens fitted")
        self.machine.refresh_from_db()
        self.assertEqual((self.machine.status, self.machine.status_note), ("working", "New lens fitted"))
        self.assertContains(self.client.get(self.url), "New lens fitted")  # in the status history


class ReasonAfterTheButtonTests(PagesTestCase):
    """The reader's out-of-order button can't take a reason, so whoever pressed it gives one on the page."""

    def setUp(self):
        super().setUp()
        trained(self.kim, self.machine)
        self.machine.status = Equipment.Status.OUT_OF_ORDER
        self.machine.save()
        MachineStatusChange.objects.create(
            equipment=self.machine, status="out_of_order", reported_by=self.kim, source="button"
        )

    def test_the_page_asks_whoever_pressed_it(self):
        response = self.client.get(self.url)
        self.assertContains(response, "You marked it out of order with the button on its reader.")
        self.client.force_login(member())
        self.assertNotContains(self.client.get(self.url), "with the button on its reader")

    def test_their_reason_is_saved_and_the_team_is_told(self):
        fixer = member(given_name="Fixer")
        self.join_team(fixer)
        self.client.post(reverse("equipment:report", args=["laser"]), {"status": "out_of_order", "note": "Fan stuck"})
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.status_note, "Fan stuck")
        self.assertIn("says what's wrong with Red laser: Fan stuck", Notification.objects.get(member=fixer).message)
        self.assertNotContains(self.client.get(self.url), "with the button on its reader")


class ServiceTests(PagesTestCase):
    def record(self, when, note="New belt"):
        return self.client.post(reverse("equipment:service", args=["laser"]), {"serviced_at": when, "note": note})

    def test_the_team_records_a_service_which_rearms_the_reminder(self):
        self.join_team(self.kim)
        self.machine.service_interval_hours = 1
        self.machine.service_reminder_sent_at = timezone.now()
        self.machine.save()
        today = timezone.localdate()
        self.record(today.isoformat())
        record = ServiceRecord.objects.get()
        self.assertEqual((record.serviced_at, record.note, record.recorded_by), (today, "New belt", self.kim))
        self.machine.refresh_from_db()
        self.assertIsNone(self.machine.service_reminder_sent_at)
        self.assertContains(self.client.get(self.url), "New belt")

    def test_everyone_sees_the_history_but_only_the_team_records(self):
        ServiceRecord.objects.create(equipment=self.machine, note="Mirrors cleaned")
        response = self.client.get(self.url)
        self.assertContains(response, "Mirrors cleaned")
        self.assertNotContains(response, "Record a service")
        self.record(timezone.localdate().isoformat())
        self.assertEqual(ServiceRecord.objects.count(), 1)

    def test_not_in_the_future(self):
        self.join_team(self.kim)
        self.record((timezone.localdate() + timedelta(days=1)).isoformat())
        self.record("not a date")
        self.assertFalse(ServiceRecord.objects.exists())

    def test_a_due_service_shows(self):
        self.machine.service_interval_hours = 1
        self.machine.save()
        who = member()
        trained(who, self.machine)
        top_up(who)
        finished_session(self.machine, who, minutes=90)
        self.assertContains(self.client.get(self.url), "Service due")


class ReclassifyTests(PagesTestCase):
    def setUp(self):
        super().setUp()
        self.user = member(given_name="Uma")
        trained(self.user, self.machine)
        top_up(self.user)
        self.session = finished_session(self.machine, self.user, minutes=60)

    def mark(self, reason="training"):
        url = reverse("equipment:reclassify", args=["laser", self.session.pk])
        return self.client.post(url, {"reason": reason})

    def test_the_team_marks_a_session_as_training_and_the_fee_goes_back_once(self):
        self.join_team(self.kim)
        self.assertEqual(credit_pence(self.user), 1700)
        self.assertContains(self.client.get(self.url), "Mark")
        self.mark()
        self.session.refresh_from_db()
        self.assertEqual((self.session.reason, self.session.fee_pence), ("training", 0))
        self.assertEqual(credit_pence(self.user), 2000)
        self.assertIn("£3.00 went back to your credit", Notification.objects.get(member=self.user).message)
        self.mark("testing")
        self.session.refresh_from_db()
        self.assertEqual(self.session.reason, "training")
        self.assertEqual(credit_pence(self.user), 2000)

    def test_only_the_team(self):
        self.assertNotContains(self.client.get(self.url), 'name="reason"')
        self.mark()
        self.session.refresh_from_db()
        self.assertEqual(self.session.reason, "")
        self.assertEqual(credit_pence(self.user), 1700)

    def test_only_training_or_testing_and_only_finished_sessions(self):
        self.join_team(self.kim)
        self.mark("maintenance")
        self.session.refresh_from_db()
        self.assertEqual(self.session.reason, "")
        open_one = rules.start_session(self.machine, self.user, close_existing=False)
        self.client.post(reverse("equipment:reclassify", args=["laser", open_one.pk]), {"reason": "training"})
        open_one.refresh_from_db()
        self.assertEqual(open_one.reason, "")

    def test_a_session_on_another_machine_is_not_found(self):
        self.join_team(self.kim)
        url = reverse("equipment:reclassify", args=["drill", self.session.pk])
        self.assertEqual(self.client.post(url, {"reason": "training"}).status_code, 404)
