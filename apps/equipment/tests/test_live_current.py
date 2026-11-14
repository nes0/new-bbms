"""Live current: a reader sends its readings while an admin watches them to choose a threshold (05 §5.4)."""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.access.models import Device
from apps.access.tests.test_api_v2 import V2TestCase
from apps.equipment import live_current
from apps.equipment.models import CurrentSample, Equipment
from apps.equipment.tests.helpers import laser
from apps.members.models import Member


class LiveCurrentApiTests(V2TestCase):
    def setUp(self):
        super().setUp()
        self.machine = laser(meter_mode=Equipment.MeterMode.CURRENT, current_threshold_amps=Decimal("2.50"))
        self.reader = self.make_device("laser", Device.Role.EQUIPMENT)

    def check_in(self):
        return self.call(self.reader, "check-in", {"event": "heartbeat", "cache_version": None}).json()

    def test_the_machine_object_says_whether_to_report_current(self):
        self.assertFalse(self.check_in()["machine"]["meter"]["report_current"])
        live_current.start_reporting(self.machine)
        self.assertTrue(self.check_in()["machine"]["meter"]["report_current"])

    def test_reporting_stops_by_itself_after_a_while(self):
        live_current.start_reporting(self.machine, now=timezone.now() - live_current.REPORT_FOR - timedelta(seconds=1))
        self.assertFalse(self.check_in()["machine"]["meter"]["report_current"])

    def test_readings_are_stored_a_second_apart_ending_now(self):
        live_current.start_reporting(self.machine)
        body = self.call(self.reader, "current", {"amps": [0.1, 3.25, 3.3]}).json()
        self.assertEqual(body, {"accepted": True, "report_current": True})
        samples = list(self.machine.current_samples.order_by("measured_at"))
        self.assertEqual([s.amps for s in samples], [Decimal("0.10"), Decimal("3.25"), Decimal("3.30")])
        self.assertEqual(samples[2].measured_at - samples[0].measured_at, timedelta(seconds=2))

    def test_readings_sent_after_reporting_stopped_are_refused_and_the_reader_is_told_to_stop(self):
        live_current.start_reporting(self.machine)
        live_current.stop_reporting(self.machine)
        body = self.call(self.reader, "current", {"amps": [1.0]}).json()
        self.assertEqual(body, {"accepted": False, "report_current": False})
        self.assertFalse(CurrentSample.objects.exists())

    def test_a_session_metered_machine_can_report_too_so_its_threshold_can_be_chosen_before_switching(self):
        self.machine.meter_mode = Equipment.MeterMode.SESSION
        self.machine.save()
        live_current.start_reporting(self.machine)
        self.assertTrue(self.call(self.reader, "current", {"amps": [1.0]}).json()["accepted"])

    def test_malformed_readings_are_a_400(self):
        live_current.start_reporting(self.machine)
        for amps in ([], [-1], ["2.5"], [True], [2000], [1.0] * 61, 2.5, None):
            response = self.call(self.reader, "current", {"amps": amps})
            self.assertEqual(response.status_code, 400, amps)
            self.assertEqual(response.json()["error"], "bad_field")

    def test_a_door_reader_gets_409(self):
        door = self.make_device("front-door", Device.Role.ENTRY)
        self.assertEqual(self.call(door, "current", {"amps": [1.0]}).status_code, 409)

    def test_starting_doesnt_change_the_config_version(self):
        version = self.machine.config_version
        live_current.start_reporting(self.machine)
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.config_version, version)

    def test_starting_again_clears_out_samples_older_than_a_day(self):
        old = timezone.now() - timedelta(days=2)
        CurrentSample.objects.create(equipment=self.machine, device=self.reader, amps=1, measured_at=old)
        CurrentSample.objects.create(equipment=self.machine, device=self.reader, amps=2, measured_at=timezone.now())
        live_current.start_reporting(self.machine)
        self.assertEqual(list(CurrentSample.objects.values_list("amps", flat=True)), [Decimal("2.00")])


class LiveCurrentAdminTests(TestCase):
    def setUp(self):
        self.admin = Member.objects.create_superuser("boss@example.com", "a-good-password")
        self.client.force_login(self.admin)
        self.machine = laser(meter_mode=Equipment.MeterMode.CURRENT, current_threshold_amps=Decimal("2.50"))
        self.reader = Device.objects.create(name="laser", device_id="laser", role=Device.Role.EQUIPMENT)
        self.url = reverse("admin:equipment_equipment_live_current", args=[self.machine.pk])

    def test_the_machine_page_links_to_it(self):
        page = self.client.get(reverse("admin:equipment_equipment_change", args=[self.machine.pk]))
        self.assertContains(page, self.url)

    def test_start_and_stop(self):
        self.client.post(self.url, {"action": "start"})
        self.machine.refresh_from_db()
        self.assertTrue(live_current.is_reporting(self.machine))
        self.assertContains(self.client.get(self.url), "Reporting until")
        self.client.post(self.url, {"action": "stop"})
        self.machine.refresh_from_db()
        self.assertFalse(live_current.is_reporting(self.machine))

    def test_saving_the_threshold_bumps_the_config_version_so_the_reader_picks_it_up(self):
        version = self.machine.config_version
        self.client.post(self.url, {"action": "threshold", "threshold": "3.1"})
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.current_threshold_amps, Decimal("3.10"))
        self.assertGreater(self.machine.config_version, version)

    def test_a_threshold_that_isnt_a_number_is_refused(self):
        response = self.client.post(self.url, {"action": "threshold", "threshold": "lots"}, follow=True)
        self.assertContains(response, "must be a number of amps")
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.current_threshold_amps, Decimal("2.50"))

    def test_the_readings_feed_gives_the_last_five_minutes_and_last_minutes_range(self):
        live_current.start_reporting(self.machine)
        now = timezone.now()
        for seconds_ago, amps in ((600, 9), (90, 5), (30, 0.2), (1, 3.4)):
            CurrentSample.objects.create(
                equipment=self.machine, device=self.reader, amps=amps, measured_at=now - timedelta(seconds=seconds_ago)
            )
        data = self.client.get(
            reverse("admin:equipment_equipment_live_current_readings", args=[self.machine.pk])
        ).json()
        self.assertEqual([amps for _, amps in data["samples"]], [5.0, 0.2, 3.4])
        self.assertEqual(data["last_minute"], {"min": 0.2, "max": 3.4})
        self.assertEqual((data["threshold"], data["reporting"]), (2.5, True))

    def test_staff_without_change_permission_cant_use_it(self):
        clerk = Member.objects.create_user("clerk@example.com", "a-good-password", is_staff=True)
        self.client.force_login(clerk)
        self.assertEqual(self.client.post(self.url, {"action": "start"}).status_code, 403)
        self.machine.refresh_from_db()
        self.assertFalse(live_current.is_reporting(self.machine))
