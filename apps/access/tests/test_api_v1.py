"""The v1 device API must behave exactly as today's front-door firmware expects (spec 05 §5.4)."""

from django.test import TestCase
from django.utils import timezone

from apps.access import tags
from apps.access.models import Device, KeyFob
from apps.members.models import Member

CARD = 12345601


class DeviceApiV1Tests(TestCase):
    def setUp(self):
        self.door = Device(name="Front door", device_id="main-door", role=Device.Role.ENTRY)
        self.key = self.door.generate_api_key()
        self.door.save()
        self.member = Member.objects.create_user(
            "kim@example.com",
            "a-good-password",
            given_name="Kim",
            family_name="Keyholder",
            status=Member.Status.ACTIVE,
            trusted=True,
            key_holder=True,
            photo_approved=True,
            induction_completed=True,
            inducted_at=timezone.now(),
            email_verified_at=timezone.now(),
        )
        self.fob = KeyFob.objects.create(member=self.member, tag_id=tags.full_tag("55", CARD))
        # Like the real door: JSON body, ApiKey header, no CSRF token or session.
        self.client = self.client_class(enforce_csrf_checks=True)

    def post(self, path, body=None, key=None):
        return self.client.post(path, body or {}, content_type="application/json", headers={"ApiKey": key or self.key})

    def tap(self, tag, device="main-door", key=None):
        return self.post("/acs/activity", {"tagId": tag, "device": device, "occurredAt": "1793914428"}, key)

    def test_keyholder_tap_returns_201_with_user_name(self):
        response = self.tap(self.fob.tag_id)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["user"]["name"], "Kim Keyholder")

    def test_check_in_is_a_4xx_so_the_door_stays_shut(self):
        self.member.key_holder = False
        self.member.save()
        response = self.tap(self.fob.tag_id)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {"error": "not_keyholder"})

    def test_unknown_fob_is_404(self):
        self.assertEqual(self.tap(tags.full_tag("55", 999)).status_code, 404)

    def test_wrong_prefix_guess_still_matches_by_card_number(self):
        self.assertEqual(self.tap(tags.full_tag("1E", CARD)).status_code, 201)

    def test_bad_or_missing_api_key_is_403(self):
        self.assertEqual(self.tap(self.fob.tag_id, key="wrong").status_code, 403)
        response = self.client.post("/acs/activity", {}, content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_key_must_belong_to_the_named_device(self):
        self.assertEqual(self.tap(self.fob.tag_id, device="someone-else").status_code, 403)

    def test_form_encoded_body_also_works(self):
        response = self.client.post(
            "/acs/activity", {"tagId": self.fob.tag_id, "device": "main-door"}, headers={"ApiKey": self.key}
        )
        self.assertEqual(response.status_code, 201)

    def test_garbage_body_is_refused_not_an_error(self):
        response = self.client.post(
            "/acs/activity", "{not json", content_type="application/json", headers={"ApiKey": self.key}
        )
        self.assertLess(response.status_code, 500)
        self.assertGreaterEqual(response.status_code, 400)

    def test_boot_and_heartbeat(self):
        self.assertEqual(self.post("/acs/node/boot").status_code, 200)
        self.assertEqual(self.post("/acs/node/heartbeat").status_code, 200)
        self.door.refresh_from_db()
        self.assertIsNotNone(self.door.last_boot)
        self.assertIsNotNone(self.door.last_heartbeat)

    def test_test_endpoint(self):
        response = self.client.get("/acs/test", headers={"ApiKey": self.key})
        self.assertEqual(response.json(), ["OK"])

    def test_status_endpoint(self):
        response = self.client.get(f"/acs/status/{self.fob.tag_id}", headers={"ApiKey": self.key})
        self.assertEqual(response.json()["user"]["key_holder"], True)
        missing = self.client.get(f"/acs/status/{tags.full_tag('55', 999)}", headers={"ApiKey": self.key})
        self.assertEqual(missing.status_code, 404)

    def test_equipment_devices_are_refused_with_4xx_for_now(self):
        laser = Device(name="Laser", device_id="laser", role=Device.Role.EQUIPMENT)
        laser_key = laser.generate_api_key()
        laser.save()
        response = self.tap(self.fob.tag_id, device="laser", key=laser_key)
        self.assertEqual(response.status_code, 422)

    def test_wrong_method_is_405(self):
        self.assertEqual(self.client.get("/acs/activity", headers={"ApiKey": self.key}).status_code, 405)
