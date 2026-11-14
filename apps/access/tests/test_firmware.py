"""Firmware updates for readers: uploading a release, choosing readers, and the device API (apps/access/firmware.py)."""

import hashlib
import io
import json
import zipfile

from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.access import firmware
from apps.access.models import Device, FirmwareRelease
from apps.equipment.tests.helpers import member

from .test_api_v2 import V2TestCase

FILES = {"reader.py": b"print('reader')\n", "version.py": b'VERSION = "0.3.0"\n', "font/big.py": b"DATA = b''\n"}


def bundle(version="0.3.0", files=None, manifest=None, name="reader.zip"):
    files = FILES if files is None else files
    if manifest is None:
        manifest = {
            "version": version,
            "files": {path: hashlib.sha256(data).hexdigest() for path, data in files.items()},
        }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, data in files.items():
            archive.writestr(path, data)
        if manifest != "none":
            archive.writestr("manifest.json", json.dumps(manifest))
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="application/zip")


def release(version="0.3.0", files=None):
    return firmware.create_release(*firmware.read_bundle(bundle(version, files)))


class BundleTests(V2TestCase):
    def test_a_good_bundle_becomes_a_release_with_its_files(self):
        made = release()
        self.assertEqual(made.version, "0.3.0")
        self.assertEqual(
            firmware.manifest(made),
            {
                "version": "0.3.0",
                "files": [
                    {"path": path, "size": len(FILES[path]), "sha256": hashlib.sha256(FILES[path]).hexdigest()}
                    for path in sorted(FILES)
                ],
            },
        )

    def test_bad_bundles_are_refused_with_a_reason(self):
        wrong_hash = {"version": "1", "files": {path: "0" * 64 for path in FILES}}
        cases = {
            "not a zip": SimpleUploadedFile("x.zip", b"hello"),
            "no manifest": bundle(manifest="none"),
            "manifest lists other files": bundle(manifest={"version": "1", "files": {}}),
            "checksum mismatch": bundle(manifest=wrong_hash),
            "bad version": bundle(version="one two"),
            "the loader": bundle(files={**FILES, "main.py": b"x"}),
            "the settings folder": bundle(files={**FILES, "data/config.py": b"x"}),
            "path outside": bundle(files={**FILES, "../evil.py": b"x"}),
            "not python": bundle(files={**FILES, "notes.txt": b"x"}),
            "empty": bundle(files={}),
        }
        for name, upload in cases.items():
            with self.subTest(name), self.assertRaises(firmware.FirmwareError):
                firmware.read_bundle(upload)

    def test_a_version_can_only_be_uploaded_once(self):
        release()
        with self.assertRaises(firmware.FirmwareError):
            release()


class DeviceApiTests(V2TestCase):
    def setUp(self):
        super().setUp()
        self.device = self.make_device("door", Device.Role.ENTRY)
        self.release = release()

    def check_in(self, version="0.2.0", **extra):
        body = {"event": "heartbeat", "firmware_version": version, "cache_version": None, **extra}
        response = self.call(self.device, "check-in", body)
        self.assertEqual(response.status_code, 200)
        self.device.refresh_from_db()
        return response.json()

    def test_nothing_is_said_about_firmware_until_a_release_is_chosen(self):
        self.assertNotIn("firmware", self.check_in())
        self.assertEqual(self.call(self.device, "firmware/manifest", method="get").status_code, 404)
        sha = hashlib.sha256(FILES["reader.py"]).hexdigest()
        self.assertEqual(self.call(self.device, f"firmware/files/{sha}", method="get").status_code, 404)

    def test_check_in_names_the_chosen_firmware_and_the_files_can_be_fetched(self):
        firmware.set_target(self.device, self.release)
        self.assertEqual(self.check_in()["firmware"], {"version": "0.3.0"})
        self.assertEqual(firmware.status(self.device), "waiting to update to 0.3.0")

        manifest = self.call(self.device, "firmware/manifest", method="get").json()
        self.assertEqual(manifest["version"], "0.3.0")
        self.assertEqual(len(manifest["files"]), 3)
        for entry in manifest["files"]:
            response = self.call(self.device, f"firmware/files/{entry['sha256']}", method="get")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, FILES[entry["path"]])
            self.assertEqual(int(response["Content-Length"]), entry["size"])
        self.assertEqual(self.call(self.device, "firmware/files/" + "0" * 64, method="get").status_code, 404)

    def test_the_firmware_endpoints_need_the_device_key(self):
        firmware.set_target(self.device, self.release)
        self.assertEqual(self.client.get("/api/devices/v2/firmware/manifest").status_code, 401)

    def test_a_reader_that_reaches_the_version_is_up_to_date(self):
        firmware.set_target(self.device, self.release)
        self.check_in("0.3.0")
        self.assertEqual(firmware.status(self.device), "up to date")

    def test_a_failure_the_reader_reports_is_shown_and_cleared_by_success(self):
        firmware.set_target(self.device, self.release)
        self.check_in(firmware_update={"version": "0.3.0", "error": "rolled back: failed to start"})
        self.assertEqual(firmware.status(self.device), "update to 0.3.0 failed: rolled back: failed to start")
        self.assertIsNotNone(self.device.firmware_update_error_at)
        self.check_in(firmware_update=None)  # nothing new to say: the failure stays on show
        self.assertIn("failed", firmware.status(self.device))
        self.check_in("0.3.0")
        self.assertEqual(firmware.status(self.device), "up to date")
        self.assertEqual(self.device.firmware_update_error, "")

    def test_a_failure_for_some_other_version_is_ignored(self):
        firmware.set_target(self.device, self.release)
        self.check_in(firmware_update={"version": "0.1.0", "error": "old news"})
        self.assertEqual(firmware.status(self.device), "waiting to update to 0.3.0")

    def test_choosing_another_release_clears_the_failure(self):
        firmware.set_target(self.device, self.release)
        self.check_in(firmware_update={"version": "0.3.0", "error": "no room"})
        firmware.set_target(self.device, release("0.3.1"))
        self.assertEqual(firmware.status(self.device), "waiting to update to 0.3.1")

    def test_firmware_update_must_be_an_object(self):
        body = {"event": "heartbeat", "firmware_version": "1", "firmware_update": "oops"}
        self.assertEqual(self.call(self.device, "check-in", body).status_code, 400)

    def test_retry_is_a_command_sent_once(self):
        self.device.queue_command("retry_firmware")
        self.assertIn("retry_firmware", self.check_in()["commands"])
        self.assertNotIn("retry_firmware", self.check_in()["commands"])


class AdminTests(V2TestCase):
    def setUp(self):
        super().setUp()
        self.client = self.client_class()
        self.admin = member(is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)
        self.reader = self.make_device("laser", Device.Role.EQUIPMENT)
        self.other = self.make_device("lathe", Device.Role.EQUIPMENT)
        Device.objects.update(firmware_version="0.2.0")
        self.old_door = self.make_device("door", Device.Role.ENTRY)  # v1 firmware: has never said its version

    def test_uploading_a_release_then_choosing_readers(self):
        response = self.client.post(
            reverse("admin:access_firmwarerelease_add"), {"bundle": bundle(), "notes": "Fixes the screen."}
        )
        made = FirmwareRelease.objects.get()
        install = reverse("admin:access_firmwarerelease_install", args=[made.pk])
        self.assertRedirects(response, install)
        self.assertEqual((made.version, made.notes, made.uploaded_by), ("0.3.0", "Fixes the screen.", self.admin))
        self.assertEqual(made.files.count(), 3)

        page = self.client.get(install)
        self.assertContains(page, "laser")
        self.assertContains(page, "lathe")
        self.assertNotContains(page, "door")  # it can't update itself

        self.client.post(install, {"devices": [self.reader.pk]})
        self.reader.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.reader.target_firmware, made)
        self.assertIsNone(self.other.target_firmware)
        self.assertContains(self.client.get(install), "waiting to update to 0.3.0")

        self.client.post(install, {"devices": [self.other.pk]})  # swap: unticking stops a pending update
        self.reader.refresh_from_db()
        self.other.refresh_from_db()
        self.assertIsNone(self.reader.target_firmware)
        self.assertEqual(self.other.target_firmware, made)

    def test_a_bad_upload_says_why_and_makes_nothing(self):
        response = self.client.post(
            reverse("admin:access_firmwarerelease_add"), {"bundle": bundle(files={"main.py": b"x"}), "notes": ""}
        )
        self.assertContains(response, "isn&#x27;t a file a release may contain")
        self.assertFalse(FirmwareRelease.objects.exists())

    def test_the_same_version_twice_is_refused(self):
        release()
        response = self.client.post(reverse("admin:access_firmwarerelease_add"), {"bundle": bundle(), "notes": ""})
        self.assertContains(response, "has been uploaded already")
        self.assertEqual(FirmwareRelease.objects.count(), 1)

    def test_the_admin_pages_open(self):
        made = release()
        firmware.set_target(self.reader, made)
        for url in (
            reverse("admin:access_firmwarerelease_changelist"),
            reverse("admin:access_firmwarerelease_change", args=[made.pk]),
            reverse("admin:access_device_changelist"),
            reverse("admin:access_device_change", args=[self.reader.pk]),
        ):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertContains(self.client.get(reverse("admin:access_device_changelist")), "waiting to update to 0.3.0")

    def test_a_release_some_reader_is_meant_to_run_cannot_be_deleted(self):
        made = release()
        firmware.set_target(self.reader, made)
        delete = reverse("admin:access_firmwarerelease_delete", args=[made.pk])
        self.assertEqual(self.client.post(delete, {"post": "yes"}).status_code, 403)
        firmware.set_target(self.reader, None)
        self.client.post(delete, {"post": "yes"})
        self.assertFalse(FirmwareRelease.objects.exists())

    def test_choosing_readers_needs_permission_to_change_devices(self):
        made = release()
        clerk = member(is_staff=True)
        clerk.user_permissions.add(
            *Permission.objects.filter(codename__in=["view_firmwarerelease", "add_firmwarerelease"])
        )
        self.client.force_login(clerk)
        install = reverse("admin:access_firmwarerelease_install", args=[made.pk])
        self.client.post(install, {"devices": [self.reader.pk]})
        self.reader.refresh_from_db()
        self.assertIsNone(self.reader.target_firmware)

    def test_changing_the_target_on_the_device_page_clears_an_old_failure(self):
        first, second = release(), release("0.3.1")
        firmware.set_target(self.reader, first)
        Device.objects.filter(pk=self.reader.pk).update(firmware_update_error="no room")
        change = reverse("admin:access_device_change", args=[self.reader.pk])
        form = self.client.get(change).context["adminform"].form
        data = {name: form[name].value() if form[name].value() is not None else "" for name in form.fields}
        data["target_firmware"] = second.pk
        self.assertEqual(self.client.post(change, data).status_code, 302)
        self.reader.refresh_from_db()
        self.assertEqual((self.reader.target_firmware, self.reader.firmware_update_error), (second, ""))
