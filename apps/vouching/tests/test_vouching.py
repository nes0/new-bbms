"""Vouching for members waiting for trusted status (spec 05 §5.10)."""

from datetime import timedelta

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.access.models import AccessEvent
from apps.equipment.models import UsageSession
from apps.equipment.tests.helpers import laser
from apps.equipment.tests.helpers import member as make_member
from apps.members.models import Member
from apps.notifications.models import Notification
from apps.site_settings.models import SiteSettings
from apps.vouching import vouching
from apps.vouching.models import ThresholdNotice, Vouch, VouchChange

NOW = timezone.now()


def tap(who, days_ago=0, kind=AccessEvent.Kind.CHECK_IN):
    return AccessEvent.objects.create(member=who, kind=kind, occurred_at=NOW - timedelta(days=days_ago))


def settings(**values):
    config = SiteSettings.load()
    for name, value in values.items():
        setattr(config, name, value)
    config.save()


class VouchingTestCase(TestCase):
    def setUp(self):
        self.kim = make_member(given_name="Kim", family_name="Lee")  # waiting
        tap(self.kim, days_ago=40)
        # Trusted here only so they aren't waiting themselves; vouchers needn't be trusted.
        self.vouchers = [make_member(given_name=name, family_name="Ray", trusted=True) for name in ("Sam", "Ali", "Jo")]
        for voucher in self.vouchers:
            tap(voucher, days_ago=2)
        self.sam, self.ali, self.jo = self.vouchers
        self.trustees = Group.objects.create(name="trustees")
        self.trustee = make_member(given_name="Tess", family_name="Trustee", trusted=True)
        self.trustees.user_set.add(self.trustee)


class WhoCanVouchTests(VouchingTestCase):
    def test_a_member_whose_fob_was_used_recently_can_vouch_trusted_or_not(self):
        self.assertEqual(vouching.why_cant_vouch(self.sam), "")
        lee = make_member()
        tap(lee, days_ago=29)
        self.assertEqual(vouching.why_cant_vouch(lee), "")

    def test_a_machine_session_counts_as_fob_activity(self):
        lee = make_member()
        UsageSession.objects.create(equipment=laser(), member=lee, started_at=NOW - timedelta(days=5))
        self.assertEqual(vouching.why_cant_vouch(lee), "")

    def test_not_in_recently_enough(self):
        lee = make_member()
        tap(lee, days_ago=31)
        self.assertIn("last 30 days", vouching.why_cant_vouch(lee))
        settings(vouch_active_days=60)
        self.assertEqual(vouching.why_cant_vouch(lee), "")

    def test_never_in_or_refused_taps(self):
        lee = make_member()
        tap(lee, kind=AccessEvent.Kind.REFUSED)
        self.assertTrue(vouching.why_cant_vouch(lee))

    def test_only_current_members(self):
        for fields in ({"status": Member.Status.LEFT}, {"status": Member.Status.SUSPENDED}, {"banned": True}):
            who = make_member(**fields)
            tap(who)
            self.assertEqual(vouching.why_cant_vouch(who), "Only current members can vouch.", fields)


class WhoIsWaitingTests(VouchingTestCase):
    def test_untrusted_current_members_who_have_been_in(self):
        never_in = make_member()
        trusted = make_member(trusted=True)
        tap(trusted)
        left = make_member(status=Member.Status.LEFT)
        tap(left)
        honorary = make_member(status=Member.Status.HONORARY)
        tap(honorary)
        waiting = set(vouching.waiting_members())
        self.assertIn(self.kim, waiting)
        self.assertIn(honorary, waiting)
        for absent in (never_in, trusted, left):
            self.assertNotIn(absent, waiting)

    def test_nobody_sees_themselves(self):
        self.assertNotIn(self.sam, set(vouching.waiting_members(self.sam)))
        with self.assertRaisesMessage(vouching.CantVouch, "yourself"):
            vouching.record(self.sam, self.sam, "vouch")


class RecordingTests(VouchingTestCase):
    def test_vouch_concern_and_withdraw_are_one_choice_per_voucher_with_a_history(self):
        vouching.record(self.kim, self.sam, "vouch")
        self.assertEqual(vouching.score(self.kim), 1)
        vouching.record(self.kim, self.sam, "concern", "  Left the laser running unattended  ")
        self.assertEqual(Vouch.objects.get().note, "Left the laser running unattended")
        self.assertEqual(vouching.score(self.kim), -1)
        vouching.record(self.kim, self.sam, "withdraw")
        self.assertEqual(vouching.score(self.kim), 0)
        self.assertFalse(Vouch.objects.exists())
        actions = list(VouchChange.objects.order_by("pk").values_list("action", flat=True))
        self.assertEqual(actions, ["vouch", "concern", "withdraw"])

    def test_repeating_a_choice_adds_nothing(self):
        vouching.record(self.kim, self.sam, "vouch")
        vouching.record(self.kim, self.sam, "vouch")
        vouching.record(self.kim, self.ali, "withdraw")  # nothing to take back
        self.assertEqual(VouchChange.objects.count(), 1)

    def test_a_vouch_keeps_no_note(self):
        vouching.record(self.kim, self.sam, "vouch", "lovely person")
        self.assertEqual(Vouch.objects.get().note, "")

    def test_cant_vouch_for_someone_not_waiting(self):
        trusted = make_member(trusted=True)
        tap(trusted)
        with self.assertRaisesMessage(vouching.CantVouch, "isn't waiting"):
            vouching.record(trusted, self.sam, "vouch")

    def test_an_inactive_member_cant_change_their_vouches_but_they_still_count(self):
        vouching.record(self.kim, self.sam, "vouch")
        AccessEvent.objects.filter(member=self.sam).update(occurred_at=NOW - timedelta(days=90))
        with self.assertRaises(vouching.CantVouch):
            vouching.record(self.kim, self.sam, "withdraw")
        self.assertEqual(vouching.score(self.kim), 1)

    def test_vouches_stop_counting_when_the_voucher_leaves_or_is_banned(self):
        for voucher in self.vouchers:
            vouching.record(self.kim, voucher, "vouch")
        Member.objects.filter(pk=self.sam.pk).update(status=Member.Status.LEFT)
        Member.objects.filter(pk=self.ali.pk).update(banned=True)
        self.assertEqual(vouching.score(self.kim), 1)
        summary = vouching.summaries([self.kim.pk])[self.kim.pk]
        self.assertEqual([v.voucher for v in summary.vouches], [self.jo])
        self.assertEqual({v.voucher for v in summary.lapsed}, {self.sam, self.ali})


class ThresholdTests(VouchingTestCase):
    def told(self):
        return Notification.objects.filter(kind="vouch_threshold")

    def test_trustees_and_admins_are_told_once_when_the_score_reaches_the_threshold(self):
        admin = make_member(is_superuser=True, is_staff=True)
        vouching.record(self.kim, self.sam, "vouch")
        vouching.record(self.kim, self.ali, "vouch")
        self.assertFalse(self.told().exists())
        vouching.record(self.kim, self.jo, "vouch")
        self.assertEqual({n.member for n in self.told()}, {self.trustee, admin})
        notice = self.told().first()
        self.assertIn("Kim Lee", notice.message)
        self.assertTrue(notice.url.endswith(f"#member-{self.kim.pk}"))
        self.assertEqual(ThresholdNotice.objects.get(member=self.kim).score, 3)
        # Falling back and reaching it again tells nobody again.
        vouching.record(self.kim, self.jo, "withdraw")
        vouching.record(self.kim, self.jo, "vouch")
        self.assertEqual(self.told().count(), 2)

    def test_concerns_count_against_and_dont_notify(self):
        settings(vouch_threshold=2)
        vouching.record(self.kim, self.sam, "vouch")
        vouching.record(self.kim, self.ali, "concern")
        vouching.record(self.kim, self.jo, "vouch")
        self.assertEqual(vouching.score(self.kim), 1)
        self.assertFalse(self.told().exists())

    def test_lowering_the_threshold_notifies_at_the_next_change(self):
        vouching.record(self.kim, self.sam, "vouch")
        settings(vouch_threshold=1)
        self.assertFalse(self.told().exists())
        self.assertEqual(vouching.reached_threshold(), {self.kim.pk})
        vouching.record(self.kim, self.ali, "vouch")
        self.assertTrue(self.told().exists())


class PageTests(VouchingTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.sam)

    def test_the_page_lists_waiting_members_with_only_my_own_choices(self):
        vouching.record(self.kim, self.ali, "concern", "secret")
        page = self.client.get(reverse("vouching:index"))
        self.assertContains(page, "Kim Lee")
        self.assertContains(page, ">Vouch</button>")
        self.assertNotContains(page, "secret")
        self.assertNotContains(page, "Ali")  # another voucher's name never shows
        self.assertNotContains(page, f'id="member-{self.sam.pk}"')  # not myself

    def test_vouching_with_htmx_swaps_the_row(self):
        url = reverse("vouching:choose", args=[self.kim.pk])
        row = self.client.post(url, {"action": "vouch"}, headers={"HX-Request": "true"})
        self.assertContains(row, "You vouched for them")
        self.assertContains(row, "Withdraw")
        self.assertEqual(vouching.score(self.kim), 1)
        row = self.client.post(url, {"action": "withdraw"}, headers={"HX-Request": "true"})
        self.assertContains(row, "Nothing from you counts")

    def test_without_htmx_it_redirects_back_to_the_member(self):
        url = reverse("vouching:choose", args=[self.kim.pk])
        response = self.client.post(url, {"action": "concern", "note": "Unsafe with the bandsaw"})
        self.assertRedirects(response, f"{reverse('vouching:index')}#member-{self.kim.pk}")
        self.assertEqual(Vouch.objects.get().value, Vouch.Value.CONCERN)

    def test_a_member_who_cant_vouch_is_told_why(self):
        lee = make_member()
        self.client.force_login(lee)
        page = self.client.get(reverse("vouching:index"))
        self.assertContains(page, "You can vouch once you&#x27;ve been in the space")
        self.assertNotContains(page, "Kim Lee")
        response = self.client.post(reverse("vouching:choose", args=[self.kim.pk]), {"action": "vouch"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Vouch.objects.exists())

    def test_not_for_former_members(self):
        self.client.force_login(make_member(status=Member.Status.LEFT))
        self.assertEqual(self.client.get(reverse("vouching:index")).status_code, 404)

    def test_the_account_page_points_active_members_here(self):
        page = self.client.get(reverse("members:account"))
        self.assertContains(page, "1 member is waiting for trusted status")


class TrusteeViewTests(VouchingTestCase):
    def test_the_key_review_shows_every_name_and_note_and_filters_by_score(self):
        admin = make_member(is_superuser=True, is_staff=True)
        self.client.force_login(admin)
        vouching.record(self.kim, self.sam, "vouch")
        vouching.record(self.kim, self.ali, "concern", "Left the laser running")
        url = reverse("admin:members_member_key_review")
        page = self.client.get(url)
        self.assertContains(page, "Sam Ray")
        self.assertContains(page, "Left the laser running")
        self.assertContains(page, f'id="member-{self.kim.pk}"')
        settings(vouch_threshold=1)
        self.assertNotContains(self.client.get(url, {"reached": "1"}), "Kim Lee")
        vouching.record(self.kim, self.jo, "vouch")
        self.assertContains(self.client.get(url, {"reached": "1"}), "Kim Lee")

    def test_the_member_admin_page_shows_the_history(self):
        admin = make_member(is_superuser=True, is_staff=True)
        self.client.force_login(admin)
        vouching.record(self.kim, self.sam, "vouch")
        vouching.record(self.kim, self.sam, "withdraw")
        page = self.client.get(reverse("admin:members_member_change", args=[self.kim.pk]))
        self.assertContains(page, "Sam Ray withdrew")
