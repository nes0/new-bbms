import tempfile

from django.contrib.auth import authenticate
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from .hashers import from_legacy_hash
from .models import Member, MemberPhoto

# Real hashes made by the legacy server's PHP (password_hash, cost 10) for throwaway test passwords.
LEGACY_HASH = "$2y$10$sEjkh81ZV4pWfRu8mhnU4OJk47Ih8KK8f6NibQt4HLuUKJh/DaVWy"  # "BBMStest2026!"
LEGACY_LONG_HASH = "$2y$10$r6Jy1run8K3C.9eLkcnk6OrkZPtwaMN18Tm02p/Q5rluJsuBJxGVG"  # "a"*72 + "TAIL-IGNORED"


def make_member(password="a-good-password", **fields):
    defaults = {"email": "kim@example.com", "given_name": "Kim", "family_name": "Keyholder"}
    defaults.update(fields)
    return Member.objects.create_user(password=password, **defaults)


class MemberModelTests(TestCase):
    def test_email_is_stored_lower_case_and_used_to_log_in(self):
        make_member(email="Kim@Example.COM", password="a-good-password")
        self.assertTrue(Member.objects.filter(email="kim@example.com").exists())
        self.assertIsNotNone(authenticate(username="kim@example.com", password="a-good-password"))

    def test_new_members_start_setting_up_without_access(self):
        member = make_member()
        self.assertEqual(member.status, Member.Status.SETTING_UP)
        self.assertFalse(member.has_space_access)

    def test_access_follows_status(self):
        member = make_member()
        for status in Member.Status:
            member.status = status
            expected = status in Member.STATUSES_WITH_ACCESS
            self.assertEqual(member.has_space_access, expected, status)

    def test_banned_members_have_no_access(self):
        member = make_member(status=Member.Status.ACTIVE, banned=True)
        self.assertFalse(member.has_space_access)

    def test_names(self):
        member = make_member()
        self.assertEqual(str(member), "Kim Keyholder")
        self.assertEqual(member.get_short_name(), "Kim")


class LegacyPasswordTests(TestCase):
    """Members migrated from the legacy system keep their passwords (spec 08 §8.2)."""

    def migrated_member(self, legacy_hash):
        member = make_member()
        member.password = from_legacy_hash(legacy_hash)
        member.save(update_fields=["password"])
        return member

    def test_legacy_password_works(self):
        self.migrated_member(LEGACY_HASH)
        self.assertIsNotNone(authenticate(username="kim@example.com", password="BBMStest2026!"))

    def test_wrong_password_is_refused(self):
        self.migrated_member(LEGACY_HASH)
        self.assertIsNone(authenticate(username="kim@example.com", password="bbmstest2026!"))

    def test_legacy_hash_is_upgraded_on_first_login(self):
        member = self.migrated_member(LEGACY_HASH)
        authenticate(username="kim@example.com", password="BBMStest2026!")
        member.refresh_from_db()
        self.assertTrue(member.password.startswith("pbkdf2_sha256$"))
        self.assertIsNotNone(authenticate(username="kim@example.com", password="BBMStest2026!"))

    def test_long_passwords_behave_like_php(self):
        # PHP ignores everything after 72 bytes, so the legacy system accepted any password sharing those 72 bytes.
        self.migrated_member(LEGACY_LONG_HASH)
        self.assertIsNotNone(authenticate(username="kim@example.com", password="a" * 72 + "TAIL-IGNORED"))

    def test_long_password_with_different_tail_matches_like_php(self):
        self.migrated_member(LEGACY_LONG_HASH)
        self.assertIsNotNone(authenticate(username="kim@example.com", password="a" * 72 + "something else"))

    def test_garbage_hash_never_matches(self):
        self.migrated_member("not-a-bcrypt-hash")
        self.assertIsNone(authenticate(username="kim@example.com", password="anything"))


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="bbms-test-media-"))
class MemberPhotoTests(TestCase):
    def test_deleting_a_member_deletes_their_photo_files(self):
        member = Member.objects.create_user("photo@example.com", "a-good-password", given_name="Pat")
        photo = MemberPhoto(member=member, kind=MemberPhoto.Kind.CURRENT)
        photo.image.save("x.png", ContentFile(b"\x89PNG..."), save=False)
        photo.thumbnail.save("y.png", ContentFile(b"\x89PNG..."), save=False)
        photo.save()
        storage, names = photo.image.storage, [photo.image.name, photo.thumbnail.name]
        self.assertTrue(all(storage.exists(name) for name in names))
        self.assertTrue(names[0].startswith("member-photos/"))
        with self.captureOnCommitCallbacks(execute=True):
            member.delete()
        self.assertFalse(any(storage.exists(name) for name in names))

    @override_settings(SPACES_BUCKET="")
    def test_deploy_check_warns_without_photo_storage(self):
        from .checks import photo_storage_is_set

        self.assertEqual([w.id for w in photo_storage_is_set(None)], ["members.W002"])
