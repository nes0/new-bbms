"""The demo site switch (DEMO_SITE) and its seed command."""

import json
import tempfile
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from apps.access.models import Device, KeyFob
from apps.equipment.models import Equipment
from apps.members.models import Member
from apps.payments.credit import credit_pence

MEDIA = tempfile.mkdtemp(prefix="bbms-test-media-")


class DemoSwitchTests(TestCase):
    @override_settings(DEBUG=False, DEMO_SITE=False)
    def test_live_site_has_no_fakes_or_banner(self):
        self.assertEqual(self.client.get("/dev/fake-device/").status_code, 404)
        self.assertEqual(self.client.get("/dev/fake-exit/").status_code, 404)
        self.assertNotContains(self.client.get("/accounts/login/"), "Demo site.")

    @override_settings(DEBUG=False, DEMO_SITE=True)
    def test_demo_site_has_fakes_and_says_so(self):
        self.assertContains(self.client.get("/dev/fake-device/"), "demo instructions")
        self.assertContains(self.client.get("/dev/fake-exit/"), "demo instructions")
        self.assertContains(self.client.get("/accounts/login/"), "Demo site.")

    def test_demo_site_keeps_fake_state_in_fakes_dir(self):
        from apps.billing import gocardless

        folder = tempfile.mkdtemp(prefix="bbms-test-fakes-")
        with override_settings(DEBUG=False, DEMO_SITE=True, FAKES_DIR=folder, TESTING=False):
            gocardless.fake()._save()
            self.assertIsInstance(gocardless.client(), gocardless.FakeGoCardless)
        gocardless.reset_fake()
        self.assertTrue((Path(folder) / ".fake-gocardless.json").exists())


@override_settings(MEDIA_ROOT=MEDIA)
class SeedDemoTests(TestCase):
    @override_settings(DEBUG=False, DEMO_SITE=False)
    def test_refused_on_a_live_site(self):
        with self.assertRaisesMessage(CommandError, "demo site only"):
            call_command("seed_demo", password="pw")

    @override_settings(DEMO_SITE=True)
    def test_seeds_an_empty_database_once(self):
        keys_file = Path(tempfile.mkdtemp()) / "keys.json"
        call_command("seed_demo", password="Shared-pw-1", keys_file=str(keys_file), stdout=open("/dev/null", "w"))
        data = json.loads(keys_file.read_text())
        self.assertEqual(len(data["members"]), Member.objects.count())
        self.assertGreaterEqual(Member.objects.count(), 20)
        ada = Member.objects.get(email="ada.lovelace@example.com")
        self.assertTrue(ada.check_password("Shared-pw-1") and ada.is_superuser)
        self.assertTrue(Member.objects.get(email="victor.frankenstein@example.com").banned)
        self.assertEqual(KeyFob.objects.count(), Member.objects.count())
        self.assertTrue(Equipment.objects.filter(archived=True).exists())
        # Every working machine has a reader, plus the front door and the exit node; the keys work.
        self.assertEqual(set(data["keys"]), set(Device.objects.values_list("device_id", flat=True)))
        for device_id, key in data["keys"].items():
            self.assertEqual(Device.for_api_key(key).device_id, device_id)
        self.assertFalse(any(m.email.split("@")[1] != "example.com" for m in Member.objects.all()))
        self.assertGreater(credit_pence(ada), -500)
        with self.assertRaisesMessage(CommandError, "already has members"):
            call_command("seed_demo", password="pw")
