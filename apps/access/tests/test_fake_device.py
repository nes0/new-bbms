"""The fake device page and its demo data are for local development only."""

from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from apps.access.models import Device, KeyFob
from apps.equipment.models import Equipment, Induction
from apps.members.models import Member


class FakeDevicePageTests(TestCase):
    def test_hidden_on_the_live_site(self):
        self.assertEqual(self.client.get("/dev/fake-device/").status_code, 404)

    @override_settings(DEBUG=True)
    def test_shown_when_debugging(self):
        response = self.client.get("/dev/fake-device/")
        self.assertContains(response, "Fake machine reader")
        self.assertContains(response, "access/fake_device.js")


class CreateDemoDeviceTests(TestCase):
    def run_command(self):
        out = StringIO()
        call_command("create_demo_device", stdout=out)
        return out.getvalue()

    def test_refuses_without_debug(self):
        with self.assertRaises(CommandError):
            self.run_command()

    @override_settings(DEBUG=True)
    def test_creates_a_working_demo_and_can_run_again(self):
        self.run_command()
        output = self.run_command()
        key = output.split("Reader API key (shown once): ")[1].split()[0]
        self.assertEqual(Device.for_api_key(key).device_id, "demo-laser")
        self.assertEqual(Equipment.objects.filter(slug="demo-laser").count(), 1)
        self.assertEqual(KeyFob.objects.filter(member__email__startswith="demo-").count(), 4)
        self.assertIn("0001000001", output)
        trained = Member.objects.get(email="demo-trained@example.com")
        self.assertTrue(Induction.objects.get(member=trained, category="demo-laser").is_trained)
        self.assertFalse(Induction.objects.filter(member__email="demo-untrained@example.com").exists())
