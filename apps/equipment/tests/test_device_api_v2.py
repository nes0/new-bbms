"""Device API v2 for machine readers (spec 09 §9.5-9.6)."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Group
from django.utils import timezone

from apps.access import tags
from apps.access.models import Device, KeyFob
from apps.access.tests.test_api_v2 import V2TestCase
from apps.equipment import rules
from apps.equipment.models import Equipment, MachineStatusChange, MeterReading, ServiceRecord, UsageSession
from apps.equipment.tests.helpers import laser, member, trained


class MachineTestCase(V2TestCase):
    def setUp(self):
        super().setUp()
        self.machine = laser()
        self.reader = self.make_device("laser", Device.Role.EQUIPMENT)
        self.kim = self.person("Kim", 100)
        self.sam = self.person("Sam", 200)

    def person(self, name, card, train=True, **fields):
        who = member(given_name=name, family_name="Test", **fields)
        KeyFob.objects.create(member=who, tag_id=tags.full_tag("70", card))
        if train:
            trained(who, self.machine)
        return who

    def tap(self, who_card="100"):
        return self.call(self.reader, "access", {"card": who_card}).json()

    def start(self, card="100"):
        body = self.tap(card)
        self.assertTrue(body["allowed"], body)
        return body["session_id"]

    def age(self, session_id, seconds):
        UsageSession.objects.filter(pk=session_id).update(started_at=timezone.now() - timedelta(seconds=seconds))


class EquipmentAccessTests(MachineTestCase):
    def test_a_trained_member_starts_a_session(self):
        body = self.tap()
        self.assertEqual((body["allowed"], body["reason"], body["resumed"]), (True, "ok", False))
        self.assertEqual(body["display_name"], "Kim T.")
        self.assertEqual(body["hourly_rate_pence"], 300)
        self.assertEqual(body["machine"]["in_use_by"], "Kim T.")
        self.assertEqual(self.machine.sessions.count(), 1)

    def test_tapping_again_after_a_reboot_resumes_the_same_session(self):
        first = self.tap()
        again = self.tap()
        self.assertTrue(again["resumed"])
        self.assertEqual(again["session_id"], first["session_id"])
        self.assertEqual(self.machine.sessions.count(), 1)

    def test_another_member_is_told_it_is_in_use_and_the_session_carries_on(self):
        first = self.tap()
        body = self.tap("200")
        self.assertEqual((body["allowed"], body["reason"], body["in_use_by"]), (False, "in_use", "Kim T."))
        self.assertIn("machine", body)
        session = self.machine.sessions.get()
        self.assertEqual((session.pk, session.is_open), (first["session_id"], True))

    def test_every_refusal_reason(self):
        self.person("Nina", 300, train=False)
        self.assertEqual(self.tap("300")["reason"], "not_trained")
        self.person("Lee", 400, status="left")
        self.assertEqual(self.tap("400")["reason"], "not_member")
        self.assertEqual(self.tap("999")["reason"], "unknown_fob")
        self.machine.status = Equipment.Status.OUT_OF_ORDER
        self.machine.save()
        self.assertEqual(self.tap("100")["reason"], "out_of_order")
        self.machine.status = Equipment.Status.WORKING
        self.machine.save()
        with mock.patch.object(rules, "member_credit_pence", return_value=-600):
            self.assertEqual(self.tap("100")["reason"], "insufficient_credit")
        self.machine.moodle_course_id = 7
        self.machine.course_required_for_existing = True
        self.machine.save()
        self.assertEqual(self.tap("100")["reason"], "course_incomplete")

    def test_team_member_on_an_out_of_order_machine_gets_a_maintenance_session(self):
        self.machine.status = Equipment.Status.OUT_OF_ORDER
        self.machine.save()
        self.kim.groups.add(Group.objects.get(name="laser"))
        body = self.tap()
        self.assertEqual((body["allowed"], body["session_reason"]), (True, "maintenance"))

    def test_a_second_start_while_one_is_open_sees_in_use(self):
        self.tap("100")
        self.assertEqual(self.tap("200")["reason"], "in_use")
        self.assertEqual(self.machine.sessions.filter(ended_at__isnull=True).count(), 1)

    def test_disabled_reader_refuses(self):
        self.reader.enabled = False
        self.reader.save()
        self.assertEqual(self.tap()["reason"], "device_disabled")


class SessionHeartbeatTests(MachineTestCase):
    def test_fee_so_far_is_at_least_the_minimum_and_balance_follows(self):
        session_id = self.start()
        with mock.patch.object(rules, "member_credit_pence", return_value=1000):
            body = self.call(self.reader, f"sessions/{session_id}/heartbeat", {}).json()
        self.assertEqual(body["open"], True)
        self.assertEqual(body["fee_so_far_pence"], 75)  # 15 minutes at £3/hour
        self.assertEqual(body["balance_pence"], 925)

    def test_fee_grows_with_time(self):
        session_id = self.start()
        self.age(session_id, 3600)
        body = self.call(self.reader, f"sessions/{session_id}/heartbeat", {}).json()
        self.assertEqual(body["fee_so_far_pence"], 300)
        self.assertGreaterEqual(body["elapsed_seconds"], 3600)

    def test_maintenance_sessions_cost_nothing(self):
        self.machine.status = Equipment.Status.OUT_OF_ORDER
        self.machine.save()
        self.kim.groups.add(Group.objects.get(name="laser"))
        session_id = self.start()
        body = self.call(self.reader, f"sessions/{session_id}/heartbeat", {}).json()
        self.assertEqual(body["fee_so_far_pence"], 0)

    def test_ended_session_says_open_false(self):
        session_id = self.start()
        self.call(self.reader, f"sessions/{session_id}/end", {"ended_by": "button"})
        response = self.call(self.reader, f"sessions/{session_id}/heartbeat", {})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["open"], body["ended_by"]), (False, "button"))

    def test_another_machines_session_is_404(self):
        other = laser(name="Blue laser", slug="blue")
        session = UsageSession.objects.create(equipment=other, member=self.kim)
        response = self.call(self.reader, f"sessions/{session.pk}/heartbeat", {})
        self.assertEqual((response.status_code, response.json()["error"]), (404, "session_not_found"))


class SessionEndTests(MachineTestCase):
    def test_tap_by_the_owner_ends_it_and_reports_the_fee(self):
        session_id = self.start()
        self.age(session_id, 3720)
        body = self.call(self.reader, f"sessions/{session_id}/end", {"card": "100"}).json()
        self.assertEqual((body["ended"], body["already_ended"], body["removed"]), (True, False, False))
        self.assertEqual(body["fee_pence"], 310)
        self.assertIsNone(body["machine"]["in_use_by"])
        self.assertEqual(UsageSession.objects.get(pk=session_id).ended_by, "tap")

    def test_tap_by_someone_else_does_not_end_it(self):
        session_id = self.start()
        body = self.call(self.reader, f"sessions/{session_id}/end", {"card": "200"}).json()
        self.assertEqual((body["ended"], body["reason"], body["in_use_by"]), (False, "in_use", "Kim T."))
        self.assertTrue(UsageSession.objects.get(pk=session_id).is_open)
        unknown = self.call(self.reader, f"sessions/{session_id}/end", {"card": "9999"}).json()
        self.assertEqual((unknown["ended"], unknown["reason"]), (False, "unknown_fob"))

    def test_button_ends_it(self):
        session_id = self.start()
        self.call(self.reader, f"sessions/{session_id}/end", {"ended_by": "button"})
        self.assertEqual(UsageSession.objects.get(pk=session_id).ended_by, "button")

    def test_neither_card_nor_button_is_400(self):
        session_id = self.start()
        self.assertEqual(self.call(self.reader, f"sessions/{session_id}/end", {}).status_code, 400)

    def test_ending_twice_is_idempotent_and_keeps_the_fee(self):
        session_id = self.start()
        self.age(session_id, 3600)
        first = self.call(self.reader, f"sessions/{session_id}/end", {"card": "100"}).json()
        self.age(session_id, 7200)  # time passing must not change a finished session
        again = self.call(self.reader, f"sessions/{session_id}/end", {"card": "100"}).json()
        self.assertTrue(again["already_ended"])
        self.assertEqual(again["fee_pence"], first["fee_pence"])

    def test_a_session_of_60_seconds_or_less_is_removed_and_free(self):
        session_id = self.start()
        body = self.call(self.reader, f"sessions/{session_id}/end", {"card": "100"}).json()
        self.assertEqual((body["removed"], body["fee_pence"]), (True, 0))


class MachineStatusTests(MachineTestCase):
    def report(self, session_id, status="out_of_order", note="Bent"):
        return self.call(
            self.reader, "machine-status", {"status": status, "session_id": session_id, "note": note}
        ).json()

    def test_report_out_of_order_then_fixed(self):
        session_id = self.start()
        start_version = self.machine.config_version
        with mock.patch.object(rules, "notify_team") as notify:
            body = self.report(session_id)
        self.assertTrue(body["changed"])
        self.machine.refresh_from_db()
        self.assertEqual((self.machine.status, self.machine.status_note), ("out_of_order", "Bent"))
        self.assertGreater(self.machine.config_version, start_version)
        change = MachineStatusChange.objects.get()
        self.assertEqual((change.source, change.reported_by, change.session_id), ("button", self.kim, session_id))
        notify.assert_called_once()
        self.assertTrue(UsageSession.objects.get(pk=session_id).is_open)  # their session carries on
        # A team member who is tapped in can mark it working again.
        self.call(self.reader, f"sessions/{session_id}/end", {"card": "100"})
        self.kim.groups.add(Group.objects.get(name="laser"))
        team_session = self.start()
        self.assertTrue(self.report(team_session, "working", "")["changed"])
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.status, "working")

    def test_same_status_changes_nothing(self):
        session_id = self.start()
        body = self.report(session_id, "working", "")
        self.assertEqual(body, {"changed": False, "reason": "no_change"})
        self.assertEqual(MachineStatusChange.objects.count(), 0)

    def test_must_be_tapped_in(self):
        self.assertEqual(self.report(999), {"changed": False, "reason": "not_tapped_in"})
        session_id = self.start()
        self.call(self.reader, f"sessions/{session_id}/end", {"ended_by": "button"})
        self.assertEqual(self.report(session_id)["reason"], "not_tapped_in")

    def test_bad_status_is_400(self):
        response = self.call(self.reader, "machine-status", {"status": "on_fire", "session_id": 1})
        self.assertEqual(response.status_code, 400)


class MeterTests(MachineTestCase):
    def setUp(self):
        super().setUp()
        self.machine.meter_mode = Equipment.MeterMode.CURRENT
        self.machine.current_threshold_amps = "2.50"
        self.machine.save()

    def send(self, seq, seconds, session_id=None):
        return self.call(self.reader, "meter", {"seq": seq, "active_seconds": seconds, "session_id": session_id})

    def test_accepted_and_added_to_the_session(self):
        session_id = self.start()
        self.assertEqual(self.send(1, 55, session_id).json(), {"accepted": True})
        self.send(2, 5, session_id)
        self.assertEqual(UsageSession.objects.get(pk=session_id).motor_on_seconds, 60)
        self.assertEqual(self.machine.meter_readings.count(), 2)

    def test_duplicate_seq_is_ignored(self):
        self.send(1, 55)
        body = self.send(1, 55).json()
        self.assertEqual(body, {"accepted": False, "reason": "duplicate"})
        self.assertEqual(self.machine.meter_readings.count(), 1)
        self.assertTrue(self.send(0, 5).json()["accepted"])  # out of order is fine

    def test_session_metered_machine_ignores_readings(self):
        self.machine.meter_mode = Equipment.MeterMode.SESSION
        self.machine.save()
        self.assertEqual(self.send(1, 55).json(), {"accepted": False, "reason": "session_metered"})
        self.assertEqual(MeterReading.objects.count(), 0)

    def test_bad_active_seconds_is_400(self):
        self.assertEqual(self.send(1, 86401).status_code, 400)
        self.assertEqual(self.send(1, -1).status_code, 400)
        self.assertEqual(self.send(1, "ten").status_code, 400)

    def test_threshold_is_sent_as_a_string(self):
        body = self.call(self.reader, "check-in", {"event": "boot"}).json()
        self.assertEqual(body["machine"]["meter"], {"mode": "current", "current_threshold_amps": "2.50"})


class RunningHoursTests(MachineTestCase):
    def test_current_mode_adds_up_readings(self):
        self.machine.meter_mode = Equipment.MeterMode.CURRENT
        MeterReading.objects.create(device=self.reader, equipment=self.machine, seq=1, active_seconds=1800)
        MeterReading.objects.create(device=self.reader, equipment=self.machine, seq=2, active_seconds=1800)
        self.assertEqual(rules.running_seconds(self.machine), 3600)

    def test_session_mode_adds_up_sessions_but_not_removed_ones(self):
        now = timezone.now()
        UsageSession.objects.create(
            equipment=self.machine,
            member=self.kim,
            started_at=now - timedelta(hours=3),
            ended_at=now - timedelta(hours=2),
        )
        UsageSession.objects.create(
            equipment=self.machine,
            member=self.kim,
            started_at=now - timedelta(hours=1),
            ended_at=now - timedelta(hours=1) + timedelta(seconds=30),
            removed=True,
        )
        self.assertEqual(rules.running_seconds(self.machine), 3600)

    def test_hours_since_service_resets_and_the_reminder_is_sent_once(self):
        self.machine.service_interval_hours = 1
        self.machine.save()
        now = timezone.now()
        UsageSession.objects.create(
            equipment=self.machine,
            member=self.kim,
            started_at=now - timedelta(hours=3),
            ended_at=now - timedelta(hours=1),
        )
        self.assertTrue(rules.service_due(self.machine))
        with mock.patch.object(rules, "notify_team") as notify:
            rules.tidy_up(now)
            rules.tidy_up(now)
        notify.assert_called_once()
        ServiceRecord.objects.create(equipment=self.machine, serviced_at=timezone.localdate() + timedelta(days=1))
        self.assertEqual(rules.hours_since_service(self.machine), 0)
        self.assertFalse(rules.service_due(self.machine))
        self.machine.refresh_from_db()
        self.assertIsNone(self.machine.service_reminder_sent_at)


class IdleAutoEndTests(MachineTestCase):
    def setUp(self):
        super().setUp()
        self.machine.meter_mode = Equipment.MeterMode.CURRENT
        self.machine.idle_auto_end_minutes = 30
        self.machine.save()

    def started_ago(self, minutes):
        session_id = self.start()
        UsageSession.objects.filter(pk=session_id).update(started_at=timezone.now() - timedelta(minutes=minutes))
        return UsageSession.objects.get(pk=session_id)

    def reading(self, seq, minutes_ago, seconds=60):
        MeterReading.objects.create(
            device=self.reader,
            equipment=self.machine,
            seq=seq,
            active_seconds=seconds,
            received_at=timezone.now() - timedelta(minutes=minutes_ago),
        )

    def test_ends_at_the_last_moment_the_motor_ran(self):
        session = self.started_ago(120)
        self.reading(1, 90)
        self.reading(2, 60)
        self.reading(3, 45, seconds=0)  # idle readings don't count
        self.assertEqual(rules.auto_end_idle_sessions(), 1)
        session.refresh_from_db()
        self.assertEqual(session.ended_by, "idle")
        self.assertAlmostEqual(session.ended_at, timezone.now() - timedelta(minutes=60), delta=timedelta(seconds=5))

    def test_ends_at_the_start_when_the_motor_never_ran(self):
        session = self.started_ago(45)
        self.assertEqual(rules.auto_end_idle_sessions(), 1)
        session.refresh_from_db()
        self.assertEqual(session.ended_at, session.started_at)

    def test_not_while_the_motor_is_running(self):
        self.started_ago(120)
        self.reading(1, 5)
        self.assertEqual(rules.auto_end_idle_sessions(), 0)

    def test_the_meter_endpoint_triggers_it_and_so_does_the_tidy_up(self):
        session = self.started_ago(120)
        self.call(self.reader, "meter", {"seq": 1, "active_seconds": 0, "session_id": session.pk})
        session.refresh_from_db()
        self.assertEqual(session.ended_by, "idle")
        self.started_ago(120)
        self.assertEqual(rules.tidy_up()["idle"], 1)
        self.assertFalse(self.machine.sessions.filter(ended_at__isnull=True).exists())

    def test_not_in_session_mode_or_when_the_option_is_off(self):
        self.started_ago(120)
        self.machine.idle_auto_end_minutes = None
        self.machine.save()
        self.assertEqual(rules.auto_end_idle_sessions(), 0)
        self.machine.idle_auto_end_minutes = 30
        self.machine.meter_mode = Equipment.MeterMode.SESSION
        self.machine.save()
        self.assertEqual(rules.auto_end_idle_sessions(), 0)
