"""Issuing fobs and enrolling them by tapping on a reader (spec 05 §5.1)."""

from datetime import timedelta

from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.utils import timezone

from apps.access import fobs, tags
from apps.access.models import Device, FobEnrolment, KeyFob
from apps.equipment.tests.helpers import laser, member
from apps.members.models import Member
from apps.site_settings.models import SiteSettings

CARD = 1234567  # printed on the fob as 0001234567
TAG = "700012D68733"


def device(device_id="main-door", role=Device.Role.ENTRY):
    reader = Device(name=device_id.replace("-", " ").capitalize(), device_id=device_id, role=role)
    key = reader.generate_api_key()
    reader.save()
    return reader, key


class ParseTests(TestCase):
    def test_full_tag_gives_tag_and_card_number(self):
        self.assertEqual(fobs.parse_fob_id(" 700012d68733 "), (TAG, CARD))

    def test_decimal_card_number(self):
        self.assertEqual(fobs.parse_fob_id("0001234567"), (None, CARD))

    def test_bad_checksum_says_what_it_should_be(self):
        with self.assertRaisesMessage(fobs.FobError, "should be 33"):
            fobs.parse_fob_id("700012D68700")

    def test_rejects_other_things(self):
        for text in ("", "BB1234", "12345678901", "9999999999", "0"):
            with self.subTest(text=text), self.assertRaises(fobs.FobError):
                fobs.parse_fob_id(text)


class IssueTests(TestCase):
    def setUp(self):
        self.member = member()

    def test_issue_to_a_current_member(self):
        fob = fobs.issue_fob(self.member, tag_id=TAG, card_number=CARD)
        self.assertEqual((fob.member, fob.tag_id, fob.card_number, fob.active), (self.member, TAG, CARD, True))

    def test_only_to_active_members(self):
        fobs.issue_fob(member(status=Member.Status.HONORARY), card_number=1)
        others = [member(status=status) for status in ("setting-up", "leaving", "suspended", "left")]
        for who in [*others, member(banned=True)]:
            with self.subTest(who=who), self.assertRaises(fobs.FobError):
                fobs.issue_fob(who, card_number=CARD)
        self.assertEqual(KeyFob.objects.count(), 1)

    def test_at_most_the_setting_and_lost_fobs_dont_count(self):
        first = fobs.issue_fob(self.member, card_number=1)
        fobs.issue_fob(self.member, card_number=2)
        with self.assertRaisesMessage(fobs.FobError, "already has 2 active fobs"):
            fobs.issue_fob(self.member, card_number=3)
        first.mark_lost()
        fobs.issue_fob(self.member, card_number=3)
        SiteSettings.objects.update_or_create(pk=1, defaults={"max_active_fobs": 3})
        fobs.issue_fob(self.member, card_number=4)

    def test_a_fob_belongs_to_one_member_even_once_lost(self):
        fob = fobs.issue_fob(self.member, tag_id=TAG, card_number=CARD)
        fob.mark_lost()
        with self.assertRaisesMessage(fobs.FobError, "reactivate it instead"):
            fobs.issue_fob(member(), card_number=CARD)

    def test_reactivating_a_found_fob_respects_the_limit(self):
        lost = fobs.issue_fob(self.member, card_number=1)
        lost.mark_lost()
        fobs.issue_fob(self.member, card_number=2)
        fobs.issue_fob(self.member, card_number=3)
        with self.assertRaises(fobs.FobError):
            fobs.reactivate_fob(lost)
        KeyFob.objects.get(card_number=3).mark_lost()
        fobs.reactivate_fob(lost)
        lost.refresh_from_db()
        self.assertTrue(lost.active)
        self.assertIsNone(lost.lost_at)


class EnrolmentTestCase(TestCase):
    def setUp(self):
        self.client = self.client_class(enforce_csrf_checks=True)
        self.door, self.door_key = device()
        self.admin = Member.objects.create_superuser(
            "admin@example.com", "a-good-password", given_name="Ada", family_name="Admin"
        )
        self.newcomer = member(given_name="Nia", family_name="Newcomer")

    def open(self, reader=None, who=None):
        return fobs.open_enrolment(who or self.newcomer, reader or self.door, self.admin)

    def v1_tap(self, tag, reader=None, key=None):
        reader = reader or self.door
        return self.client.post(
            "/acs/activity",
            {"tagId": tag, "device": reader.device_id},
            content_type="application/json",
            headers={"ApiKey": key or self.door_key},
        )

    def v2_tap(self, card, key=None):
        return self.client.post(
            "/api/devices/v2/access",
            {"card": card},
            content_type="application/json",
            headers={"Authorization": f"Bearer {key or self.door_key}"},
        )


class EnrolmentTests(EnrolmentTestCase):
    def test_one_window_per_reader(self):
        self.open()
        with self.assertRaisesMessage(fobs.FobError, "already waiting"):
            self.open(who=member())
        other, _ = device("back-door", Device.Role.EXIT)
        self.open(reader=other, who=member())

    def test_not_for_someone_who_cant_have_a_fob(self):
        with self.assertRaises(fobs.FobError):
            self.open(who=member(status=Member.Status.LEFT))

    def test_not_on_a_disabled_reader(self):
        self.door.enabled = False
        self.door.save()
        with self.assertRaisesMessage(fobs.FobError, "disabled"):
            self.open()

    def test_v1_door_captures_the_card_number_from_the_first_guess_and_stays_shut(self):
        enrolment = self.open()
        # Today's door tries vendor prefixes in turn; every guess carries the same card number.
        for prefix in ("55", "BB", "19"):
            self.assertEqual(self.v1_tap(tags.full_tag(prefix, CARD)).status_code, 404)
        enrolment.refresh_from_db()
        self.assertEqual(enrolment.state, FobEnrolment.State.CAPTURED)
        self.assertEqual((enrolment.captured_card_number, enrolment.captured_tag_id), (CARD, ""))
        self.assertFalse(KeyFob.objects.exists())

    def test_only_the_first_unknown_fob_is_captured(self):
        enrolment = self.open()
        self.v1_tap(tags.full_tag("55", CARD))
        self.v1_tap(tags.full_tag("55", 777))
        enrolment.refresh_from_db()
        self.assertEqual(enrolment.captured_card_number, CARD)

    def test_known_fobs_work_as_usual_during_a_window(self):
        keyholder = member(trusted=True, key_holder=True, photo_approved=True)
        fob = fobs.issue_fob(keyholder, card_number=123)
        enrolment = self.open()
        self.assertEqual(self.v1_tap(tags.full_tag("55", fob.card_number)).status_code, 201)
        enrolment.refresh_from_db()
        self.assertEqual(enrolment.state, FobEnrolment.State.WAITING)

    def test_a_lost_fob_isnt_captured(self):
        fobs.issue_fob(member(), card_number=CARD).mark_lost()
        enrolment = self.open()
        self.v1_tap(tags.full_tag("55", CARD))
        self.assertEqual(self.v2_tap(str(CARD)).json()["reason"], "fob_lost")
        enrolment.refresh_from_db()
        self.assertEqual(enrolment.state, FobEnrolment.State.WAITING)

    def test_nothing_is_captured_outside_a_window(self):
        enrolment = self.open()
        enrolment.expires_at = timezone.now() - timedelta(seconds=1)
        enrolment.save()
        self.v1_tap(tags.full_tag("55", CARD))
        enrolment.refresh_from_db()
        self.assertEqual(enrolment.state, FobEnrolment.State.EXPIRED)
        self.assertEqual(self.v2_tap(str(CARD)).json()["reason"], "unknown_fob")

    def test_other_readers_dont_capture(self):
        enrolment = self.open()
        other, other_key = device("back-door", Device.Role.EXIT)
        self.v1_tap(tags.full_tag("55", CARD), reader=other, key=other_key)
        enrolment.refresh_from_db()
        self.assertEqual(enrolment.state, FobEnrolment.State.WAITING)

    def test_v2_reader_says_enrolled_with_the_members_name(self):
        enrolment = self.open()
        body = self.v2_tap(TAG.lower()).json()
        self.assertEqual(body, {"allowed": False, "reason": "enrolled", "display_name": "Nia N."})
        enrolment.refresh_from_db()
        self.assertEqual((enrolment.captured_card_number, enrolment.captured_tag_id), (CARD, TAG))
        # Tapping the same fob again during the window gets the same answer.
        self.assertEqual(self.v2_tap(str(CARD)).json()["reason"], "enrolled")

    def test_v2_decimal_card_has_no_tag(self):
        enrolment = self.open()
        self.v2_tap("0001234567")
        enrolment.refresh_from_db()
        self.assertEqual((enrolment.captured_card_number, enrolment.captured_tag_id), (CARD, ""))

    def test_machine_readers_can_be_used_too(self):
        machine = laser()
        reader, key = device(machine.slug, Device.Role.EQUIPMENT)
        self.open(reader=reader)
        body = self.v2_tap(str(CARD), key=key).json()
        self.assertEqual((body["reason"], body["display_name"]), ("enrolled", "Nia N."))
        self.assertIn("machine", body)

    def test_v1_machine_reader_captures(self):
        machine = laser()
        reader, key = device(machine.slug, Device.Role.EQUIPMENT)
        enrolment = self.open(reader=reader)
        self.assertEqual(self.v1_tap(tags.full_tag("55", CARD), reader=reader, key=key).status_code, 404)
        enrolment.refresh_from_db()
        self.assertEqual(enrolment.captured_card_number, CARD)

    def test_confirm_issues_the_fob_and_the_door_then_knows_it(self):
        self.newcomer.trusted = self.newcomer.key_holder = self.newcomer.photo_approved = True
        self.newcomer.save()
        enrolment = self.open()
        self.v1_tap(tags.full_tag("55", CARD))
        fob = fobs.confirm_enrolment(enrolment, self.admin)
        self.assertEqual(
            (fob.member, fob.card_number, fob.tag_id, fob.issued_by), (self.newcomer, CARD, None, self.admin)
        )
        enrolment.refresh_from_db()
        self.assertEqual(
            (enrolment.state, enrolment.fob, enrolment.confirmed_by), (FobEnrolment.State.CONFIRMED, fob, self.admin)
        )
        # The door's first guess now matches on the card number.
        self.assertEqual(self.v1_tap(tags.full_tag("55", CARD)).status_code, 201)
        with self.assertRaises(fobs.FobError):
            fobs.confirm_enrolment(enrolment, self.admin)

    def test_confirm_checks_the_rules_again(self):
        enrolment = self.open()
        self.v1_tap(tags.full_tag("55", CARD))
        fobs.issue_fob(self.newcomer, card_number=1)
        fobs.issue_fob(self.newcomer, card_number=2)
        with self.assertRaisesMessage(fobs.FobError, "already has 2"):
            fobs.confirm_enrolment(enrolment, self.admin)

    def test_cancel(self):
        enrolment = self.open()
        fobs.cancel_enrolment(enrolment)
        self.assertEqual(enrolment.state, FobEnrolment.State.CANCELLED)
        self.v1_tap(tags.full_tag("55", CARD))
        enrolment.refresh_from_db()
        self.assertIsNone(enrolment.captured_at)
        with self.assertRaises(fobs.FobError):
            fobs.confirm_enrolment(enrolment, self.admin)
        self.open()  # the reader is free again


class AdminTests(EnrolmentTestCase):
    def setUp(self):
        super().setUp()
        self.client = self.client_class()
        self.client.force_login(self.admin)

    def test_member_page_has_the_buttons(self):
        response = self.client.get(f"/admin/members/member/{self.newcomer.pk}/change/")
        self.assertContains(response, f"/admin/access/keyfob/issue/?member={self.newcomer.pk}")
        self.assertContains(response, f"/admin/access/keyfob/enrol/?member={self.newcomer.pk}")

    def test_add_goes_to_issue(self):
        response = self.client.get("/admin/access/keyfob/add/?member=3")
        self.assertRedirects(response, "/admin/access/keyfob/issue/?member=3", fetch_redirect_response=False)

    def test_issue_a_typed_fob(self):
        url = "/admin/access/keyfob/issue/"
        self.assertContains(self.client.get(f"{url}?member={self.newcomer.pk}"), "Issue a fob")
        response = self.client.post(url, {"member": self.newcomer.pk, "fob_id": "700012d68733"})
        self.assertRedirects(response, f"/admin/members/member/{self.newcomer.pk}/change/")
        fob = KeyFob.objects.get()
        self.assertEqual((fob.member, fob.tag_id, fob.issued_by), (self.newcomer, TAG, self.admin))

    def test_issue_shows_whats_wrong(self):
        url = "/admin/access/keyfob/issue/"
        self.assertContains(
            self.client.post(url, {"member": self.newcomer.pk, "fob_id": "700012d68700"}), "should be 33"
        )
        left = member(status=Member.Status.LEFT)
        self.assertContains(
            self.client.post(url, {"member": left.pk, "fob_id": "1234567"}), "needs an active membership"
        )
        self.assertFalse(KeyFob.objects.exists())

    def test_tap_to_enrol_from_start_to_finish(self):
        response = self.client.get(f"/admin/access/keyfob/enrol/?member={self.newcomer.pk}")
        self.assertContains(response, f'<option value="{self.door.pk}" selected>')
        response = self.client.post("/admin/access/keyfob/enrol/", {"member": self.newcomer.pk, "device": self.door.pk})
        enrolment = FobEnrolment.objects.get()
        page = f"/admin/access/keyfob/enrol/{enrolment.pk}/"
        self.assertRedirects(response, page)
        waiting = self.client.get(page, headers={"HX-Request": "true"})
        self.assertContains(waiting, 'hx-trigger="every 2s"')
        self.assertNotContains(waiting, "<html")

        self.v1_tap(tags.full_tag("55", CARD))
        captured = self.client.get(page, headers={"HX-Request": "true"})
        self.assertContains(captured, "0001234567")
        self.assertNotContains(captured, "hx-trigger")

        response = self.client.post(page, {"action": "confirm"})
        self.assertRedirects(response, f"/admin/members/member/{self.newcomer.pk}/change/")
        self.assertEqual(KeyFob.objects.get().card_number, CARD)

    def test_cancel_from_the_page(self):
        enrolment = self.open()
        self.client.post(f"/admin/access/keyfob/enrol/{enrolment.pk}/", {"action": "cancel"})
        enrolment.refresh_from_db()
        self.assertEqual(enrolment.state, FobEnrolment.State.CANCELLED)

    def test_issuing_is_a_permission_that_a_role_can_be_given(self):
        comms = member(is_staff=True)
        self.client.force_login(comms)
        url = "/admin/access/keyfob/issue/"
        self.assertRedirects(self.client.get(url), "/admin/", fetch_redirect_response=False)
        group = Group.objects.create(name="comms")
        group.permissions.add(Permission.objects.get(codename="add_keyfob"))
        comms.groups.add(group)
        self.client.post(url, {"member": self.newcomer.pk, "fob_id": "1234567"})
        self.assertEqual(KeyFob.objects.get().issued_by, comms)

    def test_mark_lost_and_reactivate(self):
        fob = fobs.issue_fob(self.newcomer, card_number=CARD)
        url = "/admin/access/keyfob/"
        self.client.post(url, {"action": "mark_lost", "_selected_action": [fob.pk]})
        fob.refresh_from_db()
        self.assertFalse(fob.active)
        self.client.post(url, {"action": "reactivate", "_selected_action": [fob.pk]})
        fob.refresh_from_db()
        self.assertTrue(fob.active)

    def test_search_by_printed_number(self):
        fobs.issue_fob(self.newcomer, card_number=CARD)
        self.assertContains(self.client.get("/admin/access/keyfob/?q=0001234567"), "Newcomer")

    def test_enrolment_log_is_read_only(self):
        self.open()
        response = self.client.get("/admin/access/fobenrolment/")
        self.assertContains(response, "Newcomer")
        self.assertNotContains(response, "Add fob enrolment")
