"""Member profiles, photos, the directory and the groups page (spec 06 §6.1)."""

import io
import tempfile

from django.contrib.auth.models import Group, Permission
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image

from apps.equipment.tests.helpers import member as make_member
from apps.members.models import Member, MemberChange, MemberPhoto
from apps.notifications.models import Notification
from apps.profiles import photos
from apps.profiles.models import Profile, RoleInfo
from apps.site_settings.models import SiteSettings


def make_public():
    config = SiteSettings.load()  # the settings row is made on first use, so .update() alone may change nothing
    config.public_member_directory = True
    config.save()


def picture(width=800, height=600, fmt="JPEG", name="me.jpg"):
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(out, fmt)
    return SimpleUploadedFile(name, out.getvalue(), content_type=f"image/{fmt.lower()}")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="bbms-test-media-"))
class ProfileTestCase(TestCase):
    def setUp(self):
        self.kim = make_member(given_name="Kim", family_name="Lee")
        self.admin = Member.objects.create_superuser(
            "a@example.com", "a-good-password", given_name="A", family_name="D"
        )

    def approved_photo(self, who):
        return photos.approve(photos.upload(who, picture()), self.admin)


class ProfileEditTests(ProfileTestCase):
    def test_every_member_has_a_profile(self):
        self.assertTrue(Profile.objects.filter(member=self.kim).exists())

    def test_edit(self):
        self.client.force_login(self.kim)
        self.assertEqual(self.client.get("/account/profile/").status_code, 200)
        response = self.client.post(
            "/account/profile/",
            {
                "tagline": "  Makes things ",
                "description": "Lasers.\nAnd lathes.",
                "skills": ["welding", "laser-cutter"],
                "website": "https://example.com/kim",
                "github": "https://github.com/kim-lee/",
                "twitter": "@kimlee",
                "facebook": "",
                "irc": "kim",
                "private": "on",
            },
        )
        self.assertRedirects(response, f"/members/{self.kim.pk}/")
        profile = Profile.objects.get(member=self.kim)
        self.assertEqual((profile.tagline, profile.github, profile.twitter), ("Makes things", "kim-lee", "kimlee"))
        self.assertEqual(sorted(profile.skills), ["laser-cutter", "welding"])
        self.kim.refresh_from_db()
        self.assertTrue(self.kim.profile_private)
        page = self.client.get(f"/members/{self.kim.pk}/")
        self.assertContains(page, "https://github.com/kim-lee")
        self.assertContains(page, "Lasers.<br>And lathes.")
        self.assertContains(page, "img/skills/welding.svg")

    def test_bad_links(self):
        self.client.force_login(self.kim)
        response = self.client.post("/account/profile/", {"github": "not a username", "website": "javascript:alert(1)"})
        self.assertContains(response, "Just the username")
        self.assertContains(response, "Enter a")
        self.assertEqual(Profile.objects.get(member=self.kim).github, "")

    def test_text_is_escaped(self):
        Profile.objects.filter(member=self.kim).update(tagline="<script>x</script>", description="<b>hi</b>")
        self.client.force_login(self.kim)
        page = self.client.get(f"/members/{self.kim.pk}/")
        self.assertNotContains(page, "<script>x")
        self.assertContains(page, "&lt;b&gt;hi&lt;/b&gt;")


class PhotoTests(ProfileTestCase):
    def test_upload_crops_to_squares(self):
        self.client.force_login(self.kim)
        self.client.post("/account/profile/", {"photo": picture(1200, 600)})
        pending = MemberPhoto.objects.get(member=self.kim, kind="pending")
        with pending.image.open("rb") as file:
            self.assertEqual(Image.open(file).size, (500, 500))
        with pending.thumbnail.open("rb") as file:
            self.assertEqual(Image.open(file).size, (200, 200))
        self.kim.refresh_from_db()
        self.assertFalse(self.kim.photo_approved)
        # The member sees their waiting photo; other members don't.
        self.assertEqual(self.client.get(f"/members/{self.kim.pk}/photo/thumb/?waiting=1").status_code, 200)
        self.client.force_login(make_member())
        self.assertEqual(self.client.get(f"/members/{self.kim.pk}/photo/thumb/?waiting=1").status_code, 404)

    def test_not_a_photo(self):
        self.client.force_login(self.kim)
        fake = SimpleUploadedFile("me.jpg", b"not really a photo", content_type="image/jpeg")
        self.assertContains(self.client.post("/account/profile/", {"photo": fake}), "doesn&#x27;t look like a photo")
        gif = picture(fmt="GIF", name="me.gif")
        self.assertContains(self.client.post("/account/profile/", {"photo": gif}), "JPEG, PNG or WebP")
        self.assertFalse(MemberPhoto.objects.exists())

    def test_a_new_upload_replaces_a_waiting_one(self):
        photos.upload(self.kim, picture())
        photos.upload(self.kim, picture())
        self.assertEqual(MemberPhoto.objects.filter(member=self.kim).count(), 1)

    def test_approve_in_the_admin(self):
        old = self.approved_photo(self.kim)
        waiting = photos.upload(self.kim, picture())
        self.client.force_login(self.admin)
        queue = self.client.get("/admin/members/memberphoto/")
        self.assertContains(queue, f"/members/{self.kim.pk}/photo/thumb/?waiting=1")
        self.client.post("/admin/members/memberphoto/", {"action": "approve", "_selected_action": [waiting.pk]})
        self.assertEqual(
            list(MemberPhoto.objects.filter(member=self.kim).values_list("pk", "kind")), [(waiting.pk, "current")]
        )
        self.assertFalse(MemberPhoto.objects.filter(pk=old.pk).exists())
        self.kim.refresh_from_db()
        self.assertTrue(self.kim.photo_approved)
        self.assertTrue(MemberChange.objects.filter(member=self.kim, new_value="yes", changed_by=self.admin).exists())

    def test_decline_emails_the_member(self):
        waiting = photos.upload(self.kim, picture())
        self.client.force_login(self.admin)
        self.client.post("/admin/members/memberphoto/", {"action": "decline", "_selected_action": [waiting.pk]})
        self.assertFalse(MemberPhoto.objects.exists())
        self.assertIn("wasn't approved", mail.outbox[0].subject)

    def test_dealing_with_a_photo_clears_every_admins_notification(self):
        other_admin = Member.objects.create_superuser(
            "b@example.com", "b-good-password", given_name="B", family_name="E"
        )
        waiting = lambda: Notification.objects.filter(kind="photo_waiting", unread=True)  # noqa: E731
        photos.upload(self.kim, picture())
        self.assertEqual(set(waiting().values_list("member", flat=True)), {self.admin.pk, other_admin.pk})
        second = photos.upload(self.kim, picture())  # replaces the first: its notifications are done with
        self.assertEqual(set(waiting().values_list("key", flat=True)), {f"photo-waiting-{second.pk}"})
        photos.approve(second, self.admin)
        self.assertFalse(waiting().exists())
        third = photos.upload(self.kim, picture())
        photos.decline(third, self.admin)
        photos.upload(self.kim, picture())
        photos.remove(self.kim, MemberPhoto.Kind.PENDING, self.kim)
        self.assertFalse(waiting().exists())
        self.assertEqual(Notification.objects.filter(kind="photo_waiting").count(), 8)  # 4 uploads, 2 admins each

    def test_empty_queue_says_so(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get("/admin/members/memberphoto/"), "No photos are waiting for approval")
        photos.upload(self.kim, picture())
        self.assertNotContains(self.client.get("/admin/members/memberphoto/"), "No photos are waiting for approval")

    def test_member_deletes_their_photo(self):
        self.approved_photo(self.kim)
        self.client.force_login(self.kim)
        self.client.post("/account/profile/photo/delete/")
        self.kim.refresh_from_db()
        self.assertFalse(self.kim.photo_approved)
        self.assertFalse(MemberPhoto.objects.exists())

    def test_private_photos(self):
        self.approved_photo(self.kim)
        Profile.objects.filter(member=self.kim).update(photo_private=True)
        make_public()
        url = f"/members/{self.kim.pk}/photo/full/"
        self.assertEqual(self.client.get(url).status_code, 404)  # a visitor
        self.client.force_login(make_member())
        response = self.client.get(url)
        self.assertEqual((response.status_code, response["Content-Type"]), (200, "image/png"))
        self.assertIn("private", response["Cache-Control"])
        self.client.force_login(make_member(status=Member.Status.SUSPENDED))
        self.assertEqual(self.client.get(url).status_code, 404)


class DirectoryTests(ProfileTestCase):
    def test_members_see_everyone_listed(self):
        private = make_member(given_name="Pat", profile_private=True)
        make_member(given_name="Gone", status=Member.Status.LEFT)
        make_member(given_name="Joining", status=Member.Status.SETTING_UP)
        honorary = make_member(given_name="Hon", status=Member.Status.HONORARY)
        self.client.force_login(self.kim)
        page = self.client.get("/members/")
        for who in (self.kim, private, honorary):
            self.assertContains(page, f"/members/{who.pk}/")
        self.assertNotContains(page, "Gone ")
        self.assertNotContains(page, "Joining")

    def test_visitors(self):
        response = self.client.get("/members/")
        self.assertRedirects(response, "/accounts/login/?next=/members/", fetch_redirect_response=False)
        self.assertEqual(self.client.get(f"/members/{self.kim.pk}/").status_code, 404)
        make_public()
        private = make_member(given_name="Pat", profile_private=True)
        page = self.client.get("/members/")
        self.assertContains(page, f"/members/{self.kim.pk}/")
        self.assertNotContains(page, f"/members/{private.pk}/")
        self.assertEqual(self.client.get(f"/members/{private.pk}/").status_code, 404)

    def test_people_who_arent_members_dont_see_it(self):
        self.client.force_login(make_member(status=Member.Status.LEFT))
        self.assertEqual(self.client.get("/members/").status_code, 404)

    def test_your_own_page_before_youre_listed(self):
        joining = make_member(status=Member.Status.SETTING_UP)
        self.client.force_login(joining)
        self.assertContains(self.client.get(f"/members/{joining.pk}/"), "Only you can see this page")

    def test_search_and_skills(self):
        welder = make_member(given_name="Wendy")
        Profile.objects.filter(member=welder).update(skills=["welding"])
        self.client.force_login(self.kim)
        page = self.client.get("/members/?skill=welding")
        self.assertContains(page, "Wendy")
        self.assertNotContains(page, "Kim Lee")
        self.assertContains(self.client.get("/members/?q=lee"), "Kim Lee")

    def test_badges_and_photos(self):
        Group.objects.create(name="trustees").user_set.add(self.kim)
        self.approved_photo(self.kim)
        self.client.force_login(make_member())
        page = self.client.get("/members/")
        self.assertContains(page, "Trustee")
        self.assertContains(page, f"/members/{self.kim.pk}/photo/thumb/")


class GroupTests(ProfileTestCase):
    def test_groups_page(self):
        laser = Group.objects.create(name="laser")
        RoleInfo.objects.create(group=laser, title="Laser team", email="laser@example.com", description="Lasers.")
        hidden = Group.objects.create(name="storage-box-user")
        RoleInfo.objects.create(group=hidden, listed=False)
        laser.user_set.add(self.kim)
        self.client.force_login(make_member())
        page = self.client.get("/groups/")
        self.assertContains(page, "Laser team")
        self.assertContains(page, "mailto:laser@example.com")
        self.assertNotContains(page, "storage-box-user")
        self.assertContains(self.client.get("/groups/laser/"), "Kim Lee")
        self.assertEqual(self.client.get("/groups/storage-box-user/").status_code, 404)
        self.assertContains(self.client.get(f"/members/{self.kim.pk}/"), "/groups/laser/")

    def test_members_only(self):
        self.assertRedirects(
            self.client.get("/groups/"), "/accounts/login/?next=/groups/", fetch_redirect_response=False
        )

    def test_admin_pages(self):
        laser = Group.objects.create(name="laser")
        self.client.force_login(self.admin)
        for url in (
            "/admin/auth/group/",
            f"/admin/auth/group/{laser.pk}/change/",
            f"/admin/members/member/{self.kim.pk}/change/",
        ):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_credits(self):
        self.assertContains(self.client.get("/credits/"), "Saw by Alberto Valz Gris")

    def test_moderators_need_the_permission(self):
        waiting = photos.upload(self.kim, picture())
        staff = make_member(is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(f"/members/{self.kim.pk}/photo/thumb/?waiting=1").status_code, 404)
        staff.user_permissions.add(Permission.objects.get(codename="change_memberphoto"))
        staff = Member.objects.get(pk=staff.pk)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(f"/members/{self.kim.pk}/photo/thumb/?waiting=1").status_code, 200)
        self.assertTrue(waiting.pk)


class CameraTests(ProfileTestCase):
    """The photo boxes offer the camera (static/js/camera.js): the front one for a selfie, the rear one for ID."""

    def test_boxes_ask_for_the_camera(self):
        self.client.force_login(self.kim)
        profile = self.client.get("/account/profile/")
        self.assertContains(profile, 'data-camera="user"')
        self.assertContains(profile, "js/camera")
        documents = self.client.get("/account/id-documents/")
        self.assertContains(documents, 'data-camera="environment"', count=2)
        self.assertContains(documents, "js/camera")
