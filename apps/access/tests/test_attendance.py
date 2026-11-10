"""Attendance, hours in and the trust and key review (spec 05 §5.10)."""

from datetime import date, datetime, timedelta

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.access import attendance
from apps.access.models import AccessEvent
from apps.equipment.models import UsageSession
from apps.equipment.tests.helpers import laser, member
from apps.members.models import IdCheck, Member

TODAY = date(2026, 6, 10)


def uk(day, hour=12, minute=0):
    return timezone.make_aware(datetime(day.year, day.month, day.day, hour, minute))


def tap(who, day, kind=AccessEvent.Kind.CHECK_IN, hour=12, minute=0):
    return AccessEvent.objects.create(member=who, kind=kind, occurred_at=uk(day, hour, minute))


def visits(who, count, last=TODAY):
    """A check-in on each of `count` days, ending on `last`."""
    for days_back in range(count):
        tap(who, last - timedelta(days=days_back))


class VisitTests(TestCase):
    def setUp(self):
        self.kim = member(given_name="Kim")

    def test_a_visit_is_a_day_the_fob_was_used_at_the_door_or_on_a_machine(self):
        tap(self.kim, TODAY, AccessEvent.Kind.ENTRY, hour=10)
        tap(self.kim, TODAY, AccessEvent.Kind.CHECK_IN, hour=18)  # the same day counts once
        tap(self.kim, TODAY - timedelta(days=1), AccessEvent.Kind.CHECK_IN)
        UsageSession.objects.create(equipment=laser(), member=self.kim, started_at=uk(TODAY - timedelta(days=3)))
        record = attendance.attendance(self.kim)
        self.assertEqual(record.visits, 3)
        self.assertEqual(record.days, [TODAY, TODAY - timedelta(days=1), TODAY - timedelta(days=3)])
        self.assertEqual((record.first, record.last), (TODAY - timedelta(days=3), TODAY))

    def test_refused_taps_dont_count(self):
        tap(self.kim, TODAY, AccessEvent.Kind.REFUSED)
        self.assertEqual(attendance.attendance(self.kim).visits, 0)

    def test_tapping_out_counts_for_someone_let_in_by_another_member(self):
        tap(self.kim, TODAY, AccessEvent.Kind.EXIT)
        self.assertEqual(attendance.attendance(self.kim).days, [TODAY])

    def test_days_are_uk_days(self):
        # 23:30 UK time in summer is 22:30 UTC: still the same UK day as a tap at noon.
        tap(self.kim, TODAY, hour=12)
        tap(self.kim, TODAY, hour=23, minute=30)
        tap(self.kim, TODAY + timedelta(days=1), hour=0, minute=30)  # after midnight UK: the next day
        self.assertEqual(attendance.attendance(self.kim).visits, 2)

    def test_other_members_visits_arent_counted(self):
        visits(member(), 3)
        self.assertEqual(attendance.attendance(self.kim).visits, 0)


class StayTests(TestCase):
    """Hours in: the "Who's in" estimate (06 §6.7), with the default 4-hour timeout and 4am reset."""

    def hours(self, who, now=None):
        return attendance.hours_in(attendance.stays([who.pk], now=now or uk(TODAY, 23)).get(who.pk, []))

    def test_a_stay_runs_from_tapping_in_to_tapping_out(self):
        kim = member()
        tap(kim, TODAY, hour=18)
        tap(kim, TODAY, AccessEvent.Kind.EXIT, hour=21)
        self.assertEqual(self.hours(kim), 3)

    def test_without_tapping_out_they_are_gone_four_hours_after_their_last_fob_use(self):
        kim = member()
        tap(kim, TODAY - timedelta(days=1), hour=18)
        UsageSession.objects.create(
            equipment=laser(),
            member=kim,
            started_at=uk(TODAY - timedelta(days=1), 19),
            ended_at=uk(TODAY - timedelta(days=1), 20),
        )
        self.assertEqual(self.hours(kim), 6)  # 18:00 until 4 hours after the laser session ended

    def test_nobody_stays_past_the_daily_reset(self):
        kim = member()
        tap(kim, TODAY - timedelta(days=1), hour=23)
        tap(kim, TODAY, hour=2)  # after midnight but before the reset: the same stay, ending at 4am
        self.assertEqual(self.hours(kim), 5)

    def test_tapping_out_long_after_tapping_in_counts_the_whole_stay(self):
        kim = member()
        tap(kim, TODAY, hour=12)
        tap(kim, TODAY, AccessEvent.Kind.EXIT, hour=20)
        self.assertEqual(self.hours(kim), 8)

    def test_someone_still_in_counts_until_now(self):
        kim = member()
        tap(kim, TODAY, hour=21)
        self.assertEqual(self.hours(kim, now=uk(TODAY, 22, 30)), 1.5)


class ReviewListTests(TestCase):
    def listed(self):
        return [candidate.member for candidate in attendance.review_list(TODAY, now=uk(TODAY, 23))]

    def stay(self, who, day, start, end):
        tap(who, day, hour=start)
        tap(who, day, AccessEvent.Kind.EXIT, hour=end)

    def test_current_members_without_a_key_are_listed_most_hours_first_with_no_threshold(self):
        kim = member(given_name="Kim")
        sam = member(given_name="Sam", trusted=True)  # trusted, but no key yet
        self.stay(kim, TODAY, 18, 19)
        self.stay(sam, TODAY - timedelta(days=1), 10, 16)
        member(given_name="Ash")  # never came in
        self.assertEqual(self.listed(), [sam, kim])
        candidate = attendance.review_list(TODAY, now=uk(TODAY, 23))[1]
        self.assertEqual((candidate.attendance.visits, candidate.last_four_weeks, candidate.hours), (1, 1, 1))

    def test_key_holders_banned_and_former_members_arent_listed(self):
        for fields in (
            {"trusted": True, "key_holder": True},
            {"banned": True},
            {"status": Member.Status.LEFT},
            {"status": Member.Status.SUSPENDED},
            {"status": Member.Status.LEAVING},
        ):
            visits(member(**fields), 10)
        honorary = member(status=Member.Status.HONORARY)
        visits(honorary, 1)
        self.assertEqual(self.listed(), [honorary])

    def test_trusted_members_in_at_the_same_time_are_shown_most_hours_first(self):
        kim = member(given_name="Kim")
        self.stay(kim, TODAY, 18, 22)
        self.stay(kim, TODAY - timedelta(days=7), 18, 22)
        sam = member(given_name="Sam", trusted=True, key_holder=True)
        self.stay(sam, TODAY, 20, 23)  # 2 hours with Kim
        ash = member(given_name="Ash", trusted=True, key_holder=True)
        self.stay(ash, TODAY, 17, 23)
        self.stay(ash, TODAY - timedelta(days=7), 21, 23)  # 4 + 1 hours with Kim
        self.stay(member(given_name="Bo"), TODAY, 18, 22)  # not trusted: doesn't count
        self.stay(member(given_name="Cy", trusted=True), TODAY, 12, 14)  # not in at the same time
        candidate = next(c for c in attendance.review_list(TODAY, now=uk(TODAY, 23)) if c.member == kim)
        self.assertEqual([(seen.member, seen.hours) for seen in candidate.seen_with], [(ash, 5), (sam, 2)])

    def test_the_checks_a_key_needs_are_shown(self):
        kim = member(given_name="Kim", trusted=True, photo_on_wall=True)
        visits(kim, 1)
        candidate = attendance.review_list(TODAY)[0]
        self.assertEqual(
            candidate.checks,
            [
                ("Email confirmed", True),
                ("ID checked", False),
                ("General induction", False),
                ("Photo", True),
                ("Fob", False),
                ("Trusted", True),
                ("Key deposit", False),
            ],
        )
        self.assertFalse(candidate.ready)
        IdCheck.objects.create(member=kim, result=IdCheck.Result.APPROVED, documents_seen="photo ID")
        self.assertIn(("ID checked", True), attendance.review_list(TODAY)[0].checks)


class PageTests(TestCase):
    def setUp(self):
        self.kim = member(given_name="Kim", family_name="Kay")

    def test_members_see_their_own_visits_on_their_account_page(self):
        today = timezone.localdate()
        visits(self.kim, 3, last=today)
        self.client.force_login(self.kim)
        response = self.client.get(reverse("members:account"))
        self.assertContains(response, "3 visits since")
        self.assertContains(response, f"{today:%-d %b}")

    def test_members_with_no_visits_are_told_so(self):
        self.client.force_login(self.kim)
        self.assertContains(self.client.get(reverse("members:account")), "None yet")

    def test_admins_see_attendance_on_the_members_page_and_the_review_list(self):
        today = timezone.localdate()
        visits(self.kim, 9, last=today)
        admin = Member.objects.create_superuser("admin@example.com", "a-good-password")
        self.client.force_login(admin)
        page = self.client.get(reverse("admin:members_member_change", args=[self.kim.pk]))
        self.assertContains(page, "9 visits from")
        self.assertContains(page, "hours in")
        self.assertContains(self.client.get(reverse("admin:members_member_changelist")), "Trust and key review")
        review = self.client.get(reverse("admin:members_member_key_review"))
        self.assertContains(review, "Kim Kay")
        self.assertContains(review, "✗ ID checked")

    def test_only_those_who_can_change_members_see_the_review_list(self):
        staff = member(is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename="view_member"))
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse("admin:members_member_key_review")).status_code, 403)
