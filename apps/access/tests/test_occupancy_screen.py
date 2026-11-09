"""The occupancy screen by the door: the count on taps, who's in, and their photos (spec 05 §5.4, 06 §6.7)."""

import io
import struct
import tempfile

from django.test import override_settings
from django.utils import timezone
from PIL import Image

from apps.access.models import AccessEvent, Device
from apps.equipment.models import UsageSession
from apps.equipment.tests.helpers import laser, member
from apps.members.models import Member
from apps.profiles import photos
from apps.profiles.tests.test_profiles import picture

from .test_api_v2 import V2TestCase


def check_in(who, minutes_ago=10):
    return AccessEvent.objects.create(
        member=who, kind=AccessEvent.Kind.CHECK_IN, occurred_at=timezone.now() - timezone.timedelta(minutes=minutes_ago)
    )


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="bbms-test-media-"))
class OccupancyScreenTests(V2TestCase):
    def setUp(self):
        super().setUp()
        self.exit = self.make_device("exit", Device.Role.EXIT)
        self.door = self.make_device("main-door", Device.Role.ENTRY)
        self.admin = Member.objects.create_superuser("admin@example.com", "a-good-password")
        self.kim = member(given_name="Kim", family_name="Kay")

    def with_photo(self, who):
        photos.approve(photos.upload(who, picture()), self.admin)
        who.refresh_from_db()
        return who

    def occupancy(self, device=None, **headers):
        return self.call(device or self.exit, "occupancy", method="get", **headers)

    def photo(self, who, query="", device=None, **headers):
        return self.call(device or self.exit, f"photos/{who.pk}{query}", method="get", **headers)

    # --- the count on taps ---

    def test_door_and_exit_taps_say_how_many_are_in_after_the_tap(self):
        check_in(member())
        who, _ = self.keyholder()
        self.assertEqual(self.call(self.door, "access", {"card": "1234567"}).json()["in_space"], 2)
        self.assertEqual(self.call(self.exit, "access", {"card": "1234567"}).json()["in_space"], 1)
        self.assertEqual(self.call(self.exit, "access", {"card": "999"}).json()["in_space"], 1)  # unknown fob too

    # --- who's in ---

    def test_the_list_says_who_is_in_newest_first_with_their_short_name_and_photo_version(self):
        self.with_photo(self.kim)
        check_in(self.kim, minutes_ago=30)
        sam = member(given_name="Sam", family_name="Smith")
        check_in(sam, minutes_ago=5)
        body = self.occupancy().json()
        self.assertEqual(body["in_space"], 2)
        self.assertEqual([p["name"] for p in body["people"]], ["Sam S.", "Kim K."])
        kim = body["people"][1]
        self.assertEqual(kim["id"], self.kim.pk)
        self.assertEqual(len(kim["photo"]), 8)
        self.assertIsNone(body["people"][0]["photo"])  # Sam has no photo
        self.assertEqual(kim["using"], [])

    def test_someone_using_a_machine_is_in_and_the_machine_is_named(self):
        UsageSession.objects.create(equipment=laser(), member=self.kim, started_at=timezone.now())
        person = self.occupancy().json()["people"][0]
        self.assertEqual((person["name"], person["using"], person["since"]), ("Kim K.", ["Red laser"], None))

    def test_private_profiles_are_counted_without_a_name_photo_or_id(self):
        self.kim.profile_private = True
        self.kim.save(update_fields=["profile_private"])
        self.with_photo(self.kim)
        check_in(self.kim)
        body = self.occupancy().json()
        self.assertEqual(body["in_space"], 1)
        self.assertEqual(
            {k: v for k, v in body["people"][0].items() if k != "since"},
            {"id": None, "name": None, "using": [], "photo": None},
        )
        self.assertEqual(self.photo(self.kim).status_code, 404)

    def test_members_only_photos_arent_shown(self):
        self.with_photo(self.kim)
        self.kim.profile.photo_private = True
        self.kim.profile.save()
        check_in(self.kim)
        person = self.occupancy().json()["people"][0]
        self.assertEqual((person["name"], person["photo"]), ("Kim K.", None))
        self.assertEqual(self.photo(self.kim).json()["error"], "no_photo")

    def test_the_list_can_be_fetched_again_cheaply_until_it_changes(self):
        check_in(self.kim)
        first = self.occupancy()
        self.assertEqual(self.occupancy(**{"If-None-Match": first["ETag"]}).status_code, 304)
        check_in(member())
        changed = self.occupancy(**{"If-None-Match": first["ETag"]})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()["version"], changed["ETag"].strip('"'))

    def test_machine_readers_cant_ask(self):
        machine = laser()
        reader = self.make_device(machine.slug, Device.Role.EQUIPMENT)
        self.assertEqual(self.occupancy(reader).json()["error"], "wrong_device_role")
        self.assertEqual(self.photo(self.kim, device=reader).status_code, 409)

    def test_the_front_door_can_show_it_too(self):
        check_in(self.kim)
        self.assertEqual(self.occupancy(self.door).json()["in_space"], 1)

    # --- photos ---

    def test_photos_are_served_as_rgb565_by_default_at_the_size_asked_for(self):
        self.with_photo(self.kim)
        check_in(self.kim)
        response = self.photo(self.kim, "?size=32")
        self.assertEqual(response.status_code, 200)
        data = response.content
        self.assertEqual(struct.unpack("<HH", data[:4]), (32, 32))
        self.assertEqual(len(data), 4 + 32 * 32 * 2)
        # The test picture is (200, 30, 30): mostly red, packed as 5-6-5 bits.
        pixel = struct.unpack("<H", data[4 + 2 * (16 * 32 + 16) : 4 + 2 * (16 * 32 + 16) + 2])[0]
        self.assertEqual((pixel >> 11, (pixel >> 5) & 0x3F, pixel & 0x1F), (200 >> 3, 30 >> 2, 30 >> 3))

    def test_photos_can_be_png_or_jpeg(self):
        self.with_photo(self.kim)
        check_in(self.kim)
        for fmt, kind in (("png", "PNG"), ("jpeg", "JPEG")):
            response = self.photo(self.kim, f"?format={fmt}&size=100")
            image = Image.open(io.BytesIO(response.content))
            self.assertEqual((image.format, image.size), (kind, (100, 100)))
            self.assertEqual(response["Content-Type"], f"image/{fmt}")

    def test_a_photo_can_be_kept_until_it_changes(self):
        self.with_photo(self.kim)
        check_in(self.kim)
        first = self.photo(self.kim)
        self.assertEqual(self.photo(self.kim, **{"If-None-Match": first["ETag"]}).status_code, 304)
        self.assertIn(self.occupancy().json()["people"][0]["photo"], first["ETag"])

    def test_photos_are_only_served_while_the_member_is_in(self):
        self.with_photo(self.kim)
        self.assertEqual(self.photo(self.kim).status_code, 404)  # not in
        check_in(self.kim)
        self.assertEqual(self.photo(self.kim).status_code, 200)
        AccessEvent.objects.create(member=self.kim, kind=AccessEvent.Kind.EXIT, occurred_at=timezone.now())
        self.assertEqual(self.photo(self.kim).status_code, 404)  # tapped out
        self.assertEqual(self.call(self.exit, "photos/999999", method="get").status_code, 404)

    def test_bad_sizes_and_formats_are_refused(self):
        self.with_photo(self.kim)
        check_in(self.kim)
        for query in ("?size=8", "?size=500", "?size=big", "?format=gif"):
            self.assertEqual(self.photo(self.kim, query).json()["error"], "bad_field", query)
