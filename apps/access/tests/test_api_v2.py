"""Device API v2: cards, check-in, door access, allowed fobs and offline events (spec 09)."""

from django.test import TestCase
from django.utils import timezone

from apps.access import tags
from apps.access.decisions import find_fob_by_card
from apps.access.models import AccessEvent, Device, KeyFob
from apps.equipment.models import OfflineEvent
from apps.equipment.tests.helpers import laser, member, trained

BASE = "/api/devices/v2/"


class V2TestCase(TestCase):
    def setUp(self):
        self.client = self.client_class(enforce_csrf_checks=True)
        self.keys = {}

    def make_device(self, device_id, role):
        device = Device(name=device_id, device_id=device_id, role=role)
        self.keys[device_id] = device.generate_api_key()
        device.save()
        return device

    def call(self, device, path, body=None, method="post", **headers):
        auth = {"Authorization": f"Bearer {self.keys[device.device_id]}", **headers}
        if method == "get":
            return self.client.get(BASE + path, headers=auth)
        return self.client.post(
            BASE + path, body if body is not None else {}, content_type="application/json", headers=auth
        )

    def keyholder(self, card=1234567, **fields):
        who = member(trusted=True, key_holder=True, photo_approved=True, **fields)
        fob = KeyFob.objects.create(member=who, tag_id=tags.full_tag("70", card))
        return who, fob


class CardTests(V2TestCase):
    def setUp(self):
        super().setUp()
        self.who, self.fob = self.keyholder()

    def test_decimal_with_and_without_leading_zeros(self):
        self.assertEqual(find_fob_by_card("0001234567")[0], self.fob)
        self.assertEqual(find_fob_by_card("1234567")[0], self.fob)

    def test_full_tag_in_either_case(self):
        self.assertEqual(find_fob_by_card(self.fob.tag_id)[0], self.fob)
        self.assertEqual(find_fob_by_card(self.fob.tag_id.lower())[0], self.fob)

    def test_a_fob_enrolled_with_only_a_card_number(self):
        other = KeyFob.objects.create(member=member(), card_number=42)
        self.assertEqual(find_fob_by_card("0000000042")[0], other)

    def test_a_lost_fob_is_reported_as_lost_and_a_stranger_as_unknown(self):
        self.fob.mark_lost()
        self.assertEqual(find_fob_by_card("1234567"), (None, "fob_lost"))
        self.assertEqual(find_fob_by_card("999"), (None, "unknown_fob"))

    def test_rubbish_is_rejected(self):
        for bad in ("hello", "12345678901", "-5", "700012D6873"):
            with self.assertRaises(ValueError):
                find_fob_by_card(bad)

    def test_the_legacy_bb_rule_is_not_applied(self):
        KeyFob.objects.create(member=member(), tag_id="BB0123456789")
        self.assertEqual(find_fob_by_card("0123456789"), (None, "unknown_fob"))

    def test_bad_card_is_400_over_http(self):
        door = self.make_device("main-door", Device.Role.ENTRY)
        response = self.call(door, "access", {"card": "hello"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "bad_card")


class CheckInTests(V2TestCase):
    def test_boot_and_heartbeat_set_the_right_timestamps(self):
        door = self.make_device("main-door", Device.Role.ENTRY)
        response = self.call(
            door, "check-in", {"event": "heartbeat", "firmware_version": "1.0.3", "cache_version": None}
        )
        self.assertEqual(response.status_code, 200)
        door.refresh_from_db()
        self.assertIsNone(door.last_boot)
        self.assertIsNotNone(door.last_heartbeat)
        self.assertEqual(door.firmware_version, "1.0.3")
        self.call(door, "check-in", {"event": "boot"})
        door.refresh_from_db()
        self.assertIsNotNone(door.last_boot)

    def test_body_has_timings_and_no_machine_for_a_door(self):
        door = self.make_device("main-door", Device.Role.ENTRY)
        body = self.call(door, "check-in", {"event": "boot"}).json()
        self.assertEqual((body["heartbeat_seconds"], body["session_heartbeat_seconds"]), (300, 60))
        self.assertNotIn("machine", body)
        self.assertAlmostEqual(body["server_time"], timezone.now().timestamp(), delta=5)

    def test_unknown_event_is_400(self):
        door = self.make_device("main-door", Device.Role.ENTRY)
        self.assertEqual(self.call(door, "check-in", {"event": "dance"}).status_code, 400)

    def test_missing_or_bad_key_is_401(self):
        response = self.client.post(BASE + "check-in", {}, content_type="application/json")
        self.assertEqual(response.status_code, 401)

    def test_queued_commands_are_sent_once_then_cleared(self):
        door = self.make_device("main-door", Device.Role.ENTRY)
        version = self.call(door, "check-in", {"event": "heartbeat"}).json()["cache_version"]
        door.queue_command("reboot")
        first = self.call(door, "check-in", {"event": "heartbeat", "cache_version": version}).json()
        second = self.call(door, "check-in", {"event": "heartbeat", "cache_version": version}).json()
        self.assertEqual(first["commands"], ["reboot"])
        self.assertEqual(second["commands"], [])

    def test_a_disabled_device_is_told_to_disable(self):
        door = self.make_device("main-door", Device.Role.ENTRY)
        door.enabled = False
        door.save()
        body = self.call(door, "check-in", {"event": "heartbeat"}).json()
        self.assertIn("disable", body["commands"])

    def test_refresh_cache_when_the_device_holds_an_old_version(self):
        door = self.make_device("main-door", Device.Role.ENTRY)
        body = self.call(door, "check-in", {"event": "boot", "cache_version": "stale000"}).json()
        self.assertIn("refresh_cache", body["commands"])
        again = self.call(door, "check-in", {"event": "heartbeat", "cache_version": body["cache_version"]}).json()
        self.assertNotIn("refresh_cache", again["commands"])

    def test_machine_payload_only_for_equipment_readers(self):
        machine = laser()
        reader = self.make_device("laser", Device.Role.EQUIPMENT)
        body = self.call(reader, "check-in", {"event": "boot"}).json()
        self.assertEqual(body["machine"]["name"], "Red laser")
        self.assertEqual(body["machine"]["hourly_rate_pence"], 300)
        self.assertEqual(body["machine"]["config_version"], machine.config_version)

    def test_equipment_reader_without_equipment_is_409(self):
        reader = self.make_device("ghost", Device.Role.EQUIPMENT)
        response = self.call(reader, "access", {"card": "1"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "no_equipment")


class ConfigVersionTests(TestCase):
    def test_goes_up_for_rate_status_and_threshold_but_not_description(self):
        machine = laser()
        start = machine.config_version
        machine.description = "A big red laser"
        machine.save()
        self.assertEqual(machine.config_version, start)
        machine.usage_cost_pence_per_hour = 400
        machine.save()
        self.assertEqual(machine.config_version, start + 1)
        machine.status = "out_of_order"
        machine.save()
        machine.current_threshold_amps = "2.50"
        machine.save()
        self.assertEqual(machine.config_version, start + 3)
        machine.save()  # unchanged
        machine.refresh_from_db()
        self.assertEqual(machine.config_version, start + 3)


class DoorAccessTests(V2TestCase):
    def setUp(self):
        super().setUp()
        self.door = self.make_device("main-door", Device.Role.ENTRY)
        self.exit = self.make_device("exit", Device.Role.EXIT)
        self.who, self.fob = self.keyholder()

    def test_keyholder_is_let_in_with_a_short_name(self):
        body = self.call(self.door, "access", {"card": "0001234567"}).json()
        self.assertEqual((body["allowed"], body["reason"]), (True, "ok"))
        self.assertEqual(body["display_name"], "Mo M.")
        self.assertEqual(AccessEvent.objects.get().kind, "entry")

    def test_non_keyholder_member_is_checked_in_and_the_door_stays_shut(self):
        self.who.key_holder = False
        self.who.save()
        body = self.call(self.door, "access", {"card": self.fob.tag_id}).json()
        self.assertEqual((body["allowed"], body["reason"]), (False, "checked_in"))
        self.assertEqual(AccessEvent.objects.get().kind, "check-in")

    def test_left_member_is_not_a_member(self):
        self.who.status = "left"
        self.who.save()
        body = self.call(self.door, "access", {"card": "1234567"}).json()
        self.assertEqual((body["allowed"], body["reason"]), (False, "not_member"))

    def test_exit_reader_checks_out(self):
        body = self.call(self.exit, "access", {"card": "1234567"}).json()
        self.assertEqual((body["allowed"], body["reason"]), (True, "checked_out"))

    def test_unknown_and_lost_fobs(self):
        self.assertEqual(self.call(self.door, "access", {"card": "77"}).json()["reason"], "unknown_fob")
        self.fob.mark_lost()
        body = self.call(self.door, "access", {"card": "1234567"}).json()
        self.assertEqual((body["reason"], body["display_name"]), ("fob_lost", None))

    def test_disabled_device_refuses_everyone(self):
        self.door.enabled = False
        self.door.save()
        body = self.call(self.door, "access", {"card": "1234567"}).json()
        self.assertEqual((body["allowed"], body["reason"]), (False, "device_disabled"))

    def test_door_reader_cannot_use_machine_endpoints(self):
        self.assertEqual(self.call(self.door, "meter", {"seq": 1, "active_seconds": 1}).status_code, 409)
        self.assertEqual(self.call(self.door, "sessions/1/end", {"ended_by": "button"}).status_code, 409)


class AllowedFobsTests(V2TestCase):
    def setUp(self):
        super().setUp()
        self.door = self.make_device("main-door", Device.Role.ENTRY)
        self.who, self.fob = self.keyholder()
        self.non_keyholder = member()
        KeyFob.objects.create(member=self.non_keyholder, tag_id=tags.full_tag("70", 77))

    def test_door_lists_only_keyholders_sorted_by_card(self):
        body = self.call(self.door, "allowed-fobs", method="get").json()
        self.assertEqual([f["card"] for f in body["fobs"]], ["0001234567"])
        self.assertEqual(body["fobs"][0]["tag"], self.fob.tag_id)

    def test_equipment_lists_trained_members_and_ignores_machine_status(self):
        machine = laser(status="out_of_order")
        reader = self.make_device("laser", Device.Role.EQUIPMENT)
        trained(self.non_keyholder, machine)
        body = self.call(reader, "allowed-fobs", method="get").json()
        self.assertEqual([f["card"] for f in body["fobs"]], ["0000000077"])

    def test_losing_a_fob_or_leaving_changes_the_version(self):
        before = self.call(self.door, "allowed-fobs", method="get").json()["cache_version"]
        self.fob.mark_lost()
        after = self.call(self.door, "allowed-fobs", method="get").json()
        self.assertNotEqual(before, after["cache_version"])
        self.assertEqual(after["fobs"], [])

        other, _ = self.keyholder(card=9)
        added = self.call(self.door, "allowed-fobs", method="get").json()["cache_version"]
        other.status = "left"
        other.save()
        removed = self.call(self.door, "allowed-fobs", method="get").json()["cache_version"]
        self.assertNotEqual(added, removed)

    def test_revoked_training_removes_the_fob(self):
        machine = laser()
        reader = self.make_device("laser", Device.Role.EQUIPMENT)
        induction = trained(self.non_keyholder, machine)
        self.assertEqual(len(self.call(reader, "allowed-fobs", method="get").json()["fobs"]), 1)
        induction.trained_at = None
        induction.save()
        self.assertEqual(self.call(reader, "allowed-fobs", method="get").json()["fobs"], [])

    def test_matching_etag_gives_304(self):
        response = self.call(self.door, "allowed-fobs", method="get")
        etag = response["ETag"]
        again = self.call(self.door, "allowed-fobs", method="get", **{"If-None-Match": etag})
        self.assertEqual(again.status_code, 304)


class OfflineEventTests(V2TestCase):
    def setUp(self):
        super().setUp()
        self.door = self.make_device("main-door", Device.Role.ENTRY)
        self.who, self.fob = self.keyholder()
        self.when = int(timezone.now().timestamp()) - 3600

    def send(self, device, events):
        return self.call(device, "offline-events", {"events": events})

    def door_event(self, event_id="a-1", **extra):
        return {"id": event_id, "type": "door", "card": "1234567", "occurred_at": self.when, "allowed": True, **extra}

    def test_door_event_is_recorded_as_delayed(self):
        body = self.send(self.door, [self.door_event()]).json()
        self.assertEqual(body["results"], [{"id": "a-1", "result": "recorded"}])
        event = AccessEvent.objects.get()
        self.assertTrue(event.delayed)
        self.assertEqual(event.kind, "entry")
        self.assertEqual(int(event.occurred_at.timestamp()), self.when)

    def test_refused_door_event_is_recorded_as_refused(self):
        self.send(self.door, [self.door_event(allowed=False)])
        self.assertEqual(AccessEvent.objects.get().kind, "refused")

    def test_resending_a_batch_creates_nothing_new(self):
        self.send(self.door, [self.door_event()])
        body = self.send(self.door, [self.door_event()]).json()
        self.assertEqual(body["results"][0]["result"], "duplicate")
        self.assertEqual(AccessEvent.objects.count(), 1)
        self.assertEqual(OfflineEvent.objects.count(), 1)

    def test_unknown_card_does_not_fail_the_batch(self):
        body = self.send(self.door, [self.door_event("x", card="12"), self.door_event("y")]).json()
        self.assertEqual([r["result"] for r in body["results"]], ["unknown_fob", "recorded"])

    def test_more_than_500_events_is_400(self):
        events = [self.door_event(str(i)) for i in range(501)]
        self.assertEqual(self.send(self.door, events).status_code, 400)

    def test_session_event_creates_a_finished_charged_session(self):
        machine = laser()
        reader = self.make_device("laser", Device.Role.EQUIPMENT)
        event = {
            "id": "s-1",
            "type": "session",
            "card": "1234567",
            "started_at": self.when,
            "ended_at": self.when + 3600,
            "ended_by": "tap",
            "motor_on_seconds": 3100,
        }
        self.assertEqual(self.send(reader, [event]).json()["results"][0]["result"], "recorded")
        session = machine.sessions.get()
        self.assertEqual((session.fee_pence, session.motor_on_seconds, session.ended_by), (300, 3100, "tap"))
        overlapping = {**event, "id": "s-2", "started_at": self.when + 600, "ended_at": self.when + 1200}
        self.assertEqual(self.send(reader, [overlapping]).json()["results"][0]["result"], "overlap")
        self.assertEqual(machine.sessions.count(), 1)
