"""Attendance and the trusted-status review list (spec 05 §5.10)."""

from datetime import date, datetime, timedelta

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.access import attendance
from apps.access.models import AccessEvent
from apps.equipment.models import UsageSession
from apps.equipment.tests.helpers import laser, member
from apps.members.models import Member
from apps.site_settings.models import SiteSettings

TODAY = date(2026, 6, 10)


def uk(day, hour=12, minute=0):
    return timezone.make_aware(datetime(day.year, day.month, day.day, hour, minute))


def tap(who, day, kind=AccessEvent.Kind.CHECK_IN, hour=12, minute=0):
    return AccessEvent.objects.create(member=who, kind=kind, occurred_at=uk(day, hour, minute))


def visits(who, count, last=TODAY):
    """A check-in on each of `count` days, ending on `last`."""
    for days_back in range(count):
        tap(who, last - timedelta(days=days_back))


def joined(who, weeks):
    who.date_joined = uk(TODAY) - timedelta(weeks=weeks)
    who.save(update_fields=["date_joined"])
    return who


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


class ReviewListTests(TestCase):
    def setUp(self):
        config = SiteSettings.load()
        config.trusted_review_visits, config.trusted_review_weeks = 8, 4
        config.save()

    def listed(self):
        return [candidate.member for candidate in attendance.review_list(TODAY)]

    def test_members_with_enough_visits_who_joined_long_enough_ago_are_listed_most_visits_first(self):
        kim = joined(member(given_name="Kim"), weeks=6)
        sam = joined(member(given_name="Sam"), weeks=10)
        visits(kim, 8)
        visits(sam, 12)
        self.assertEqual(self.listed(), [sam, kim])
        candidate = attendance.review_list(TODAY)[1]
        self.assertEqual((candidate.attendance.visits, candidate.last_four_weeks), (8, 8))

    def test_too_few_visits_or_too_new_isnt_listed(self):
        visits(joined(member(), weeks=6), 7)
        visits(joined(member(), weeks=3), 20)
        self.assertEqual(self.listed(), [])

    def test_trusted_banned_and_former_members_arent_listed(self):
        for fields in (
            {"trusted": True},
            {"banned": True},
            {"status": Member.Status.LEFT},
            {"status": Member.Status.SUSPENDED},
            {"status": Member.Status.LEAVING},
        ):
            visits(joined(member(**fields), weeks=10), 10)
        honorary = joined(member(status=Member.Status.HONORARY), weeks=10)
        visits(honorary, 10)
        self.assertEqual(self.listed(), [honorary])

    def test_the_threshold_is_a_setting(self):
        kim = joined(member(), weeks=2)
        visits(kim, 3)
        config = SiteSettings.load()
        config.trusted_review_visits, config.trusted_review_weeks = 3, 1
        config.save()
        self.assertEqual(self.listed(), [kim])


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
        self.kim.date_joined = timezone.now() - timedelta(weeks=8)
        self.kim.save(update_fields=["date_joined"])
        admin = Member.objects.create_superuser("admin@example.com", "a-good-password")
        self.client.force_login(admin)
        page = self.client.get(reverse("admin:members_member_change", args=[self.kim.pk]))
        self.assertContains(page, "9 visits from")
        self.assertContains(self.client.get(reverse("admin:members_member_changelist")), "Trusted-status review")
        review = self.client.get(reverse("admin:members_member_trusted_review"))
        self.assertContains(review, "Kim Kay")

    def test_only_those_who_can_change_members_see_the_review_list(self):
        staff = member(is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename="view_member"))
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse("admin:members_member_trusted_review")).status_code, 403)
