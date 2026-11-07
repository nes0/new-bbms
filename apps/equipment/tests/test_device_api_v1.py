"""The laser's reader uses the v1 API today; it must behave as the legacy system did (spec 05 §5.4)."""

from django.test import TestCase

from apps.access import tags
from apps.access.models import Device, KeyFob
from apps.equipment.models import UsageSession

from .helpers import laser, member, trained

CARD = 22223333


class EquipmentApiV1Tests(TestCase):
    def setUp(self):
        self.laser = laser()
        self.device = Device(name="Laser reader", device_id="laser", role=Device.Role.EQUIPMENT)
        self.key = self.device.generate_api_key()
        self.device.save()
        self.who = member()
        trained(self.who, self.laser)
        self.fob = KeyFob.objects.create(member=self.who, tag_id=tags.full_tag("55", CARD))

    def call(self, method, path, body=None):
        return getattr(self.client, method)(
            path, body or {}, content_type="application/json", headers={"ApiKey": self.key}
        )

    def tap(self):
        return self.call("post", "/acs/activity", {"tagId": self.fob.tag_id, "device": "laser"})

    def test_tap_starts_a_session(self):
        response = self.tap()
        self.assertEqual(response.status_code, 201)
        session = UsageSession.objects.get(pk=response.json()["activityId"])
        self.assertEqual(session.member, self.who)
        self.assertTrue(session.is_open)

    def test_heartbeat_and_end(self):
        session_id = self.tap().json()["activityId"]
        self.assertEqual(self.call("put", f"/acs/activity/{session_id}", {"tagId": self.fob.tag_id}).status_code, 200)
        self.assertEqual(self.call("delete", f"/acs/activity/{session_id}").status_code, 204)
        session = UsageSession.objects.get(pk=session_id)
        self.assertFalse(session.is_open)
        self.assertEqual(session.ended_by, UsageSession.EndedBy.DEVICE)

    def test_finished_session_is_400(self):
        session_id = self.tap().json()["activityId"]
        self.call("delete", f"/acs/activity/{session_id}")
        self.assertEqual(self.call("delete", f"/acs/activity/{session_id}").status_code, 400)
        self.assertEqual(self.call("put", f"/acs/activity/{session_id}").status_code, 400)

    def test_untrained_member_is_refused_with_422(self):
        other = member()
        fob = KeyFob.objects.create(member=other, tag_id=tags.full_tag("55", CARD + 1))
        response = self.call("post", "/acs/activity", {"tagId": fob.tag_id, "device": "laser"})
        self.assertEqual((response.status_code, response.json()), (422, {"error": "not_trained"}))

    def test_unknown_fob_is_404(self):
        response = self.call("post", "/acs/activity", {"tagId": tags.full_tag("55", 1), "device": "laser"})
        self.assertEqual(response.status_code, 404)

    def test_reader_without_matching_equipment_is_refused(self):
        orphan = Device(name="Orphan", device_id="orphan", role=Device.Role.EQUIPMENT)
        orphan_key = orphan.generate_api_key()
        orphan.save()
        response = self.client.post(
            "/acs/activity",
            {"tagId": self.fob.tag_id, "device": "orphan"},
            content_type="application/json",
            headers={"ApiKey": orphan_key},
        )
        self.assertEqual(response.status_code, 422)

    def test_sessions_on_another_machine_are_not_reachable(self):
        session_id = self.tap().json()["activityId"]
        other = Device(name="Lathe", device_id="lathe", role=Device.Role.EQUIPMENT)
        other_key = other.generate_api_key()
        other.save()
        laser(slug="lathe", name="Lathe")
        response = self.client.delete(f"/acs/activity/{session_id}", headers={"ApiKey": other_key})
        self.assertEqual(response.status_code, 404)
