from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.access import tags
from apps.access.decisions import Outcome, decide, find_fob
from apps.access.models import AccessEvent, Device, KeyFob
from apps.members.models import Member

CARD = 12345601


def keyholder(**fields):
    defaults = dict(
        given_name="Kim",
        family_name="Keyholder",
        status=Member.Status.ACTIVE,
        trusted=True,
        key_holder=True,
        photo_approved=True,
    )
    defaults.update(fields)
    email = defaults.pop("email", "kim@example.com")
    return Member.objects.create_user(email, "a-good-password", **defaults)


class FobLookupTests(TestCase):
    def setUp(self):
        self.member = keyholder()

    def test_exact_tag(self):
        fob = KeyFob.objects.create(member=self.member, tag_id=tags.full_tag("55", CARD))
        self.assertEqual(find_fob(tags.full_tag("55", CARD).lower()), fob)

    def test_card_number_matches_any_valid_prefix(self):
        fob = KeyFob.objects.create(member=self.member, tag_id=tags.full_tag("55", CARD))
        self.assertEqual(fob.card_number, CARD)
        self.assertEqual(find_fob(tags.full_tag("1E", CARD)), fob)

    def test_fob_enrolled_with_card_number_only(self):
        fob = KeyFob.objects.create(member=self.member, card_number=CARD)
        self.assertEqual(find_fob(tags.full_tag("BB", CARD)), fob)

    def test_bad_checksum_doesnt_match_by_card_number(self):
        KeyFob.objects.create(member=self.member, card_number=CARD)
        bad = tags.full_tag("55", CARD)[:10] + "00"
        self.assertIsNone(find_fob(bad))

    def test_lost_fobs_are_not_found(self):
        fob = KeyFob.objects.create(member=self.member, tag_id=tags.full_tag("55", CARD))
        fob.mark_lost()
        self.assertIsNone(find_fob(tags.full_tag("55", CARD)))

    def test_legacy_bb_rule(self):
        fob = KeyFob.objects.create(member=self.member, tag_id="BBABCDEF1234")
        self.assertEqual(find_fob("ABCDEF1234"), fob)

    def test_no_partial_matching(self):
        KeyFob.objects.create(member=self.member, tag_id="ABCDEF123456")
        self.assertIsNone(find_fob("BCDEF123456"))


class DoorDecisionTests(TestCase):
    def setUp(self):
        self.door = Device(name="Front door", device_id="main-door", role=Device.Role.ENTRY)
        self.door.generate_api_key()
        self.door.save()

    def tap(self, member, device=None, **kwargs):
        KeyFob.objects.get_or_create(member=member, card_number=CARD + member.pk)
        return decide(device or self.door, tags.full_tag("55", CARD + member.pk), **kwargs)

    def test_keyholder_opens_the_door_and_entry_is_logged(self):
        member = keyholder()
        decision = self.tap(member)
        self.assertEqual(decision.outcome, Outcome.OPEN)
        self.assertTrue(AccessEvent.objects.filter(member=member, kind=AccessEvent.Kind.ENTRY).exists())

    def test_photo_on_wall_counts(self):
        self.assertEqual(self.tap(keyholder(photo_approved=False, photo_on_wall=True)).outcome, Outcome.OPEN)

    def test_member_who_isnt_a_keyholder_checks_in(self):
        member = keyholder(trusted=False, key_holder=False)
        decision = self.tap(member)
        self.assertEqual((decision.outcome, decision.reason), (Outcome.CHECKED_IN, "not_keyholder"))
        self.assertTrue(AccessEvent.objects.filter(member=member, kind=AccessEvent.Kind.CHECK_IN).exists())

    def test_keyholder_without_photo_checks_in(self):
        decision = self.tap(keyholder(photo_approved=False))
        self.assertEqual((decision.outcome, decision.reason), (Outcome.CHECKED_IN, "no_photo"))

    def test_leaving_and_honorary_members_still_get_in(self):
        for i, status in enumerate([Member.Status.LEAVING, Member.Status.HONORARY]):
            member = keyholder(email=f"m{i}@example.com", status=status)
            self.assertEqual(self.tap(member).outcome, Outcome.OPEN, status)

    def test_former_and_suspended_members_are_refused(self):
        for i, status in enumerate([Member.Status.LEFT, Member.Status.SUSPENDED, Member.Status.SETTING_UP]):
            member = keyholder(email=f"m{i}@example.com", status=status)
            decision = self.tap(member)
            self.assertEqual((decision.outcome, decision.reason), (Outcome.REFUSED, "not_member"), status)

    def test_banned_keyholder_is_refused(self):
        self.assertEqual(self.tap(keyholder(banned=True)).outcome, Outcome.REFUSED)

    def test_unknown_fob_is_refused_and_not_logged(self):
        decision = decide(self.door, tags.full_tag("55", 999))
        self.assertEqual((decision.outcome, decision.reason), (Outcome.REFUSED, "unknown_fob"))
        self.assertFalse(AccessEvent.objects.exists())

    def test_exit_reader(self):
        exit_reader = Device(name="Exit", device_id="exit", role=Device.Role.EXIT)
        exit_reader.generate_api_key()
        exit_reader.save()
        member = keyholder(trusted=False, key_holder=False)
        self.assertEqual(self.tap(member, device=exit_reader).outcome, Outcome.CHECKED_OUT)
        self.assertTrue(AccessEvent.objects.filter(member=member, kind=AccessEvent.Kind.EXIT).exists())

    def test_door_prefix_guesses_are_recorded_once(self):
        # Today's door sends up to 13 guesses per tap; all match by card number, but it's one visit.
        member = keyholder(trusted=False, key_holder=False)
        KeyFob.objects.create(member=member, card_number=CARD)
        for prefix in ["55", "BB", "19", "17", "70"]:
            self.assertEqual(decide(self.door, tags.full_tag(prefix, CARD)).outcome, Outcome.CHECKED_IN)
        self.assertEqual(AccessEvent.objects.filter(member=member).count(), 1)

    def test_late_reports_are_marked_delayed_and_future_times_capped(self):
        member = keyholder()
        self.tap(member, occurred_at=timezone.now() - timedelta(minutes=10))
        self.assertTrue(AccessEvent.objects.get(member=member).delayed)
        AccessEvent.objects.all().delete()
        self.tap(member, occurred_at=timezone.now() + timedelta(hours=1))
        event = AccessEvent.objects.get(member=member)
        self.assertFalse(event.delayed)
        self.assertLessEqual(event.occurred_at, timezone.now())
