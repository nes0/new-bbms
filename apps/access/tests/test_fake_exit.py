"""The fake exit node page and its demo data are for local development only (spec 09 §9.8)."""

import tempfile
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from apps.access import screen
from apps.access.models import Device


class FakeExitPageTests(TestCase):
    def test_hidden_on_the_live_site(self):
        self.assertEqual(self.client.get("/dev/fake-exit/").status_code, 404)

    @override_settings(DEBUG=True)
    def test_shown_when_debugging(self):
        response = self.client.get("/dev/fake-exit/")
        self.assertContains(response, "Fake exit node")
        self.assertContains(response, "access/fake_exit.js")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="bbms-test-media-"))
class CreateDemoExitTests(TestCase):
    def run_command(self):
        out = StringIO()
        call_command("create_demo_exit", stdout=out)
        return out.getvalue()

    def test_refuses_without_debug(self):
        with self.assertRaises(CommandError):
            self.run_command()

    @override_settings(DEBUG=True)
    def test_makes_two_readers_and_a_screen_full_of_people_and_can_run_again(self):
        self.run_command()
        output = self.run_command()
        exit_key = output.split("Exit node API key (shown once):")[1].split()[0]
        door_key = output.split("Front door API key (optional):")[1].split()[0]
        self.assertEqual(Device.for_api_key(exit_key).role, Device.Role.EXIT)
        self.assertEqual(Device.for_api_key(door_key).role, Device.Role.ENTRY)
        people = screen.people()
        self.assertEqual(len(people), 5)
        names = [p["name"] for p in people]
        self.assertIn(None, names)  # the private profile
        mel = next(p for p in people if p["name"] == "Mel M.")
        self.assertIsNone(mel["photo"])  # members-only photo
        kim = next(p for p in people if p["name"] == "Kim K.")
        self.assertIsNotNone(kim["photo"])
        self.assertEqual(next(p for p in people if p["name"] == "Ari A.")["using"], ["Demo lathe"])
