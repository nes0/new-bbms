"""Who is told what, by the rest of BBMS (spec 06 §6.5)."""

import io
import tempfile
from datetime import timedelta

from django.contrib.auth.models import Group
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from apps.access.models import Device
from apps.access.monitoring import check_devices
from apps.equipment import rules
from apps.equipment.tests.helpers import laser
from apps.members import changes, id_documents
from apps.members.models import Address, Member
from apps.notifications.models import Notification
from apps.profiles import photos


def person(email, **fields):
    return Member.objects.create_user(email, "a-good-password", given_name=email.split("@")[0].title(), **fields)


def told(member, kind):
    return list(Notification.objects.filter(member=member, kind=kind))


class TrustedStatusTests(TestCase):
    def test_becoming_trusted_is_said_once_even_if_it_happens_again(self):
        admin = person("admin@example.com", is_superuser=True)
        kim = person("kim@example.com")
        for trusted in (True, False, True):
            before = changes.snapshot(kim)
            kim.trusted = trusted
            kim.save()
            changes.record(kim, before, changed_by=admin)
        (notification,) = told(kim, "trusted_status")
        self.assertEqual(notification.url, "/pay/")
        self.assertEqual(len(mail.outbox), 2)  # the email is sent each time, as before


class AddressNoticeTests(TestCase):
    def setUp(self):
        self.admin = person("admin@example.com", is_superuser=True, is_staff=True)
        self.trustee = person("trustee@example.com")
        self.trustee.groups.add(Group.objects.create(name="trustees"))
        self.bystander = person("bystander@example.com")
        self.kim = person("kim@example.com", phone="01273 123456", emergency_contact="Sam")
        self.old = Address.objects.create(member=self.kim, line_1="1 Old Road", postcode="BN1 1AA", approved=True)

    def change_address(self):
        self.client.force_login(self.kim)
        self.client.post("/account/address/", {"line_1": "2 New Street", "postcode": "bn2 2bb"})
        self.client.force_login(self.admin)
        return self.kim.pending_address

    def test_the_trustees_and_admins_are_told_there_is_one_to_approve(self):
        self.change_address()
        for who in (self.trustee, self.admin):
            (notification,) = told(who, "address_waiting")
            self.assertIn("Kim", notification.message)
            self.assertEqual(notification.url, "/admin/members/address/?approved__exact=0")
        self.assertEqual(told(self.bystander, "address_waiting"), [])
        self.assertEqual(told(self.kim, "address_waiting"), [])

    def test_the_member_is_told_when_it_is_approved(self):
        pending = self.change_address()
        self.client.post("/admin/members/address/", {"action": "approve", "_selected_action": [pending.pk]})
        self.assertEqual(len(told(self.kim, "address_approved")), 1)

    def test_the_member_is_told_when_it_is_declined(self):
        pending = self.change_address()
        self.client.post("/admin/members/address/", {"action": "decline", "_selected_action": [pending.pk]})
        (notification,) = told(self.kim, "address_declined")
        self.assertIn("wasn't accepted", notification.message)

    def test_replacing_your_own_change_doesnt_say_it_was_declined(self):
        self.change_address()
        self.change_address()
        self.assertEqual(told(self.kim, "address_declined"), [])


class IdCheckNoticeTests(TestCase):
    def setUp(self):
        self.trustee = person("trustee@example.com", is_superuser=True)
        self.kim = person("kim@example.com")

    def test_approved(self):
        id_documents.record_check(self.kim, self.trustee, "approved", "passport")
        self.assertEqual(len(told(self.kim, "id_approved")), 1)
        self.assertIn("has been checked", mail.outbox[0].subject)
        self.assertNotIn("passport", mail.outbox[0].body)  # what was seen is for the trustees

    def test_not_accepted_passes_on_the_trustees_note(self):
        id_documents.record_check(self.kim, self.trustee, "rejected", "The bill is from 2019.")
        (notification,) = told(self.kim, "id_rejected")
        self.assertIn("The bill is from 2019.", notification.message)
        self.assertEqual(notification.url, "/account/id-documents/")
        self.assertIn("The bill is from 2019.", mail.outbox[0].body)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="bbms-test-media-"))
class PhotoNoticeTests(TestCase):
    def test_the_admins_are_told_when_a_photo_is_waiting(self):
        admin = person("admin@example.com", is_superuser=True)
        kim = person("kim@example.com")
        out = io.BytesIO()
        Image.new("RGB", (800, 600), (200, 30, 30)).save(out, "JPEG")
        photos.upload(kim, SimpleUploadedFile("me.jpg", out.getvalue(), content_type="image/jpeg"))
        (notification,) = told(admin, "photo_waiting")
        self.assertIn("Kim", notification.message)
        self.assertEqual(notification.url, "/admin/members/memberphoto/?kind__exact=pending")
        self.assertEqual(told(kim, "photo_waiting"), [])


class MachineTeamTests(TestCase):
    def test_the_machines_team_is_told_and_nobody_else(self):
        machine = laser()
        team_member = person("team@example.com")
        team_member.groups.add(machine.team)
        person("other@example.com")
        rules.notify_team(machine, "Kim reported Red laser out of order.")
        (notification,) = Notification.objects.all()
        self.assertEqual(notification.member, team_member)
        self.assertIn("/admin/equipment/equipment/", notification.url)

    def test_a_machine_with_no_team_tells_the_admins(self):
        machine = laser(team=None)
        admin = person("admin@example.com", is_superuser=True)
        rules.notify_team(machine, "Red laser is due for a service.")
        self.assertEqual(told(admin, "equipment_team")[0].message, "Red laser is due for a service.")


class DeviceOfflineTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.acs = person("acs@example.com")
        self.acs.groups.add(Group.objects.create(name="acs"))
        self.admin = person("admin@example.com", is_superuser=True)
        self.door = Device(name="Front door", device_id="main-door", role="entry", monitor_heartbeat=True)
        self.door.set_api_key("made-up-key")
        self.door.last_heartbeat = self.now - timedelta(hours=2)
        self.door.save()

    def test_a_quiet_device_is_reported_once_to_the_acs_team_and_admins(self):
        self.assertEqual(check_devices(self.now), 2)
        self.assertEqual(check_devices(self.now + timedelta(minutes=15)), 0)
        (notification,) = told(self.acs, "device_offline")
        self.assertIn("Front door", notification.message)
        self.assertEqual(len(told(self.admin, "device_offline")), 1)

    def test_it_is_reported_again_after_coming_back_and_going_quiet_again(self):
        check_devices(self.now)
        self.door.last_heartbeat = self.now + timedelta(minutes=5)
        self.door.save()
        self.assertEqual(check_devices(self.now + timedelta(minutes=10)), 0)  # not quiet yet
        self.assertEqual(check_devices(self.now + timedelta(hours=2)), 2)

    def test_devices_that_arent_monitored_or_were_never_heard_from_are_left_alone(self):
        self.door.monitor_heartbeat = False
        self.door.save()
        Device.objects.create(name="New", device_id="new", role="entry", monitor_heartbeat=True, api_key_hash="x")
        self.assertEqual(check_devices(self.now), 0)

    def test_a_recent_heartbeat_is_fine(self):
        self.door.last_heartbeat = self.now - timedelta(minutes=50)
        self.door.save()
        self.assertEqual(check_devices(self.now), 0)
