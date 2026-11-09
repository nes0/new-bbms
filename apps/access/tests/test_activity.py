"""Who's in the space, and the activity page (spec 06 §6.7)."""

from datetime import datetime, time, timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.access import activity
from apps.access.models import AccessEvent
from apps.equipment.models import UsageSession
from apps.equipment.tests.helpers import laser, member
from apps.members.models import Member
from apps.site_settings.models import SiteSettings

NOW = timezone.make_aware(datetime(2026, 6, 10, 15, 0))  # a Wednesday afternoon, UK time
ENTRY, CHECK_IN, EXIT, REFUSED = (
    AccessEvent.Kind.ENTRY,
    AccessEvent.Kind.CHECK_IN,
    AccessEvent.Kind.EXIT,
    AccessEvent.Kind.REFUSED,
)


def tap(who, kind, at, **fields):
    return AccessEvent.objects.create(member=who, kind=kind, occurred_at=at, **fields)


def ago(hours=0, minutes=0, now=NOW):
    return now - timedelta(hours=hours, minutes=minutes)


def settings(**values):
    config = SiteSettings.load()
    for name, value in values.items():
        setattr(config, name, value)
    config.save()


class InSpaceTests(TestCase):
    def setUp(self):
        self.kim, self.sam = member(given_name="Kim"), member(given_name="Sam")

    def in_now(self, now=NOW):
        return [p.member for p in activity.in_space(now)]

    def test_in_until_they_tap_out(self):
        tap(self.kim, ENTRY, ago(2))
        tap(self.sam, CHECK_IN, ago(1))
        self.assertEqual(self.in_now(), [self.sam, self.kim])  # most recent arrival first
        tap(self.kim, EXIT, ago(minutes=10))
        self.assertEqual(self.in_now(), [self.sam])
        tap(self.kim, CHECK_IN, ago(minutes=5))  # back again
        presence = activity.in_space(NOW)[0]
        self.assertEqual((presence.member, presence.since), (self.kim, ago(minutes=5)))

    def test_gone_after_the_timeout(self):
        tap(self.kim, ENTRY, ago(5))
        self.assertEqual(self.in_now(), [])
        settings(occupancy_timeout_hours=6)
        self.assertEqual(self.in_now(), [self.kim])

    def test_machine_use_keeps_them_in(self):
        tap(self.kim, ENTRY, ago(7))
        UsageSession.objects.create(
            equipment=laser(), member=self.kim, started_at=ago(4), ended_at=ago(3, 30)
        )  # last seen 3½ hours ago: still in
        presence = activity.in_space(NOW)[0]
        self.assertEqual((presence.since, presence.using), (ago(7), []))
        self.assertEqual(self.in_now(ago(minutes=-31)), [])  # 4 hours after the session ended

    def test_using_a_machine_without_tapping_in(self):
        UsageSession.objects.create(equipment=laser(), member=self.sam, started_at=ago(minutes=20))
        presence = activity.in_space(NOW)[0]
        self.assertEqual((presence.member, presence.since, presence.using), (self.sam, None, ["Red laser"]))

    def test_everyone_gone_at_the_reset_hour(self):
        late = timezone.make_aware(datetime(2026, 6, 10, 3, 30))
        tap(self.kim, ENTRY, timezone.make_aware(datetime(2026, 6, 10, 2, 0)))
        self.assertEqual(self.in_now(late), [self.kim])  # before 4am: still the night before
        self.assertEqual(self.in_now(late + timedelta(minutes=45)), [])

    def test_refusals_and_repeat_taps(self):
        tap(self.kim, REFUSED, ago(minutes=5))
        self.assertEqual(self.in_now(), [])
        tap(self.sam, CHECK_IN, ago(2))
        tap(self.sam, CHECK_IN, ago(1))
        self.assertEqual(activity.in_space(NOW)[0].since, ago(2))


class PageTests(TestCase):
    def setUp(self):
        now = timezone.localtime()
        # Put the reset two to three hours ago, so it never falls inside the test's last hour.
        settings(occupancy_reset_hour=(now - timedelta(hours=3)).hour)
        self.kim = member(given_name="Kim", family_name="Lee")
        self.sam = member(given_name="Sam", family_name="Ray", status=Member.Status.LEAVING)

    def test_who_is_in_and_the_day(self):
        # Midday, so the taps below are all today whenever the tests run (just after midnight they weren't).
        now = timezone.make_aware(datetime.combine(timezone.localdate(), time(12, 0)))
        clock = mock.patch("django.utils.timezone.now", return_value=now)
        clock.start()
        self.addCleanup(clock.stop)
        tap(self.kim, ENTRY, now - timedelta(minutes=30))
        tap(self.sam, CHECK_IN, now - timedelta(minutes=20), delayed=True)
        tap(self.sam, REFUSED, now - timedelta(minutes=10), reason="not a keyholder")
        UsageSession.objects.create(
            equipment=laser(),
            member=self.kim,
            started_at=now - timedelta(minutes=25),
            ended_at=now - timedelta(minutes=5),
        )
        self.client.force_login(self.kim)
        page = self.client.get("/activity/")
        self.assertContains(page, "In the space now: 2")
        self.assertContains(page, "Opened the front door")
        self.assertContains(page, "Checked in at the door")
        self.assertContains(page, "Used the Red laser")
        self.assertContains(page, "(20 min)")
        self.assertContains(page, "recorded offline")
        self.assertNotContains(page, "not a keyholder")
        self.assertContains(page, f"/members/{self.kim.pk}/")
        self.assertNotContains(page, f"/members/{self.sam.pk}/")  # leaving: not in the directory
        self.assertContains(page, "Sam Ray")
        # The panel refreshes itself.
        panel = self.client.get("/activity/in-now/")
        self.assertContains(panel, 'hx-trigger="every 60s"')
        self.assertNotContains(panel, "<html")

    def test_other_days(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        tap(
            self.kim, ENTRY, timezone.make_aware(datetime.combine(yesterday, datetime.min.time())) + timedelta(hours=19)
        )
        self.client.force_login(self.kim)
        page = self.client.get(f"/activity/?date={yesterday:%Y-%m-%d}")
        self.assertContains(page, "Opened the front door")
        self.assertContains(page, f"?date={timezone.localdate():%Y-%m-%d}")  # a link on to today
        future = self.client.get(f"/activity/?date={timezone.localdate() + timedelta(days=3):%Y-%m-%d}")
        self.assertEqual(future.context["date"], timezone.localdate())
        self.assertEqual(self.client.get("/activity/?date=nonsense").status_code, 200)

    def test_members_only(self):
        self.assertRedirects(
            self.client.get("/activity/"), "/accounts/login/?next=/activity/", fetch_redirect_response=False
        )
        self.client.force_login(member(status=Member.Status.LEFT))
        self.assertEqual(self.client.get("/activity/").status_code, 404)
        self.assertEqual(self.client.get("/activity/in-now/").status_code, 404)

    def test_public_count(self):
        tap(self.kim, ENTRY, timezone.now() - timedelta(minutes=5))
        self.assertEqual(self.client.get("/occupancy.json").status_code, 404)
        self.assertNotContains(self.client.get("/"), "in the space now")
        settings(public_occupancy_count=True)
        response = self.client.get("/occupancy.json")
        self.assertEqual(response.json()["in_space"], 1)
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        home = self.client.get("/")
        self.assertContains(home, "1 person is in the space now.")
        self.assertNotContains(home, "Kim")
