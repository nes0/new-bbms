"""Photos of equipment (spec 05 §5.7): uploaded in the admin, shown to members on the machine's page."""

import io
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from apps.equipment import photos
from apps.equipment.admin import EquipmentPhotoForm
from apps.equipment.models import EquipmentPhoto
from apps.members.models import Member

from .helpers import laser, member


def made_up_photo(width=1600, height=1200, fmt="JPEG"):
    out = io.BytesIO()
    Image.new("RGB", (width, height), (40, 90, 200)).save(out, fmt)
    return SimpleUploadedFile(f"laser.{fmt.lower()}", out.getvalue(), content_type=f"image/{fmt.lower()}")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="bbms-test-media-"))
class EquipmentPhotoTests(TestCase):
    def setUp(self):
        self.laser = laser()

    def test_a_photo_is_cropped_to_a_square_with_a_thumbnail(self):
        photo = photos.add(self.laser, made_up_photo())
        self.assertEqual(Image.open(photo.image).size, (1000, 1000))
        self.assertEqual(Image.open(photo.thumbnail).size, (300, 300))
        self.assertEqual(photos.add(self.laser, made_up_photo(fmt="PNG")).position, 1)

    def test_something_that_isnt_a_photo_is_refused(self):
        with self.assertRaises(photos.BadPhoto):
            photos.add(self.laser, SimpleUploadedFile("notes.txt", b"not a photo"))
        self.assertFalse(EquipmentPhoto.objects.exists())

    def test_the_admin_form_adds_a_photo_and_only_moves_a_stored_one(self):
        form = EquipmentPhotoForm(
            data={"position": "0"}, files={"upload": made_up_photo()}, instance=EquipmentPhoto(equipment=self.laser)
        )
        self.assertTrue(form.is_valid(), form.errors)
        photo = form.save()
        self.assertEqual(Image.open(photo.image).size, (1000, 1000))
        # A stored photo is only moved: a file sent with it is ignored.
        image = photo.image.name
        again = EquipmentPhotoForm(data={"position": "2"}, files={"upload": made_up_photo()}, instance=photo)
        self.assertTrue(again.is_valid(), again.errors)
        self.assertEqual((again.save().image.name, photo.position), (image, 2))
        empty = EquipmentPhotoForm(data={"position": "3"}, instance=EquipmentPhoto(equipment=self.laser))
        self.assertIn("Choose a photo", str(empty.errors))

    def test_members_see_the_photos_on_the_machines_page(self):
        photo = photos.add(self.laser, made_up_photo())
        self.client.force_login(member())
        page = self.client.get(reverse("equipment:detail", args=["laser"]))
        thumb = reverse("equipment:photo", args=["laser", photo.pk, "thumb"])
        self.assertContains(page, thumb)
        response = self.client.get(thumb)
        self.assertEqual((response.status_code, response["Content-Type"]), (200, "image/jpeg"))
        self.assertEqual(self.client.get(reverse("equipment:photo", args=["laser", photo.pk, "big"])).status_code, 404)

    def test_only_members_see_the_photos(self):
        photo = photos.add(self.laser, made_up_photo())
        url = reverse("equipment:photo", args=["laser", photo.pk, "full"])
        self.assertEqual(self.client.get(url).status_code, 302)  # to log in
        self.client.force_login(member(status=Member.Status.LEFT))
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_equipment_admin_page_shows_the_photos(self):
        photo = photos.add(self.laser, made_up_photo())
        self.client.force_login(Member.objects.create_superuser("admin@example.com", "a-good-password"))
        page = self.client.get(reverse("admin:equipment_equipment_change", args=[self.laser.pk]))
        self.assertContains(page, reverse("equipment:photo", args=["laser", photo.pk, "thumb"]))
        self.assertContains(page, "cropped to a square")

    def test_more_photos_can_be_uploaded_in_the_admin_once_there_is_one(self):
        photos.add(self.laser, made_up_photo())
        self.client.force_login(Member.objects.create_superuser("admin@example.com", "a-good-password"))
        page = self.client.get(reverse("admin:equipment_equipment_change", args=[self.laser.pk]))
        self.assertContains(page, 'enctype="multipart/form-data"')
        self.assertContains(page, "Stored. To replace it")

    def test_deleting_a_photo_deletes_its_files(self):
        photo = photos.add(self.laser, made_up_photo())
        storage, names = photo.image.storage, (photo.image.name, photo.thumbnail.name)
        with self.captureOnCommitCallbacks(execute=True):
            photo.delete()
        self.assertFalse(any(storage.exists(name) for name in names))


class CameraTests(TestCase):
    def test_admin_photo_rows_offer_the_camera(self):
        admin = Member.objects.create_superuser("cam@example.com", "a-good-password", given_name="A", family_name="D")
        self.client.force_login(admin)
        page = self.client.get(reverse("admin:equipment_equipment_add"))
        self.assertContains(page, 'data-camera="environment"')
        self.assertContains(page, "js/camera")
