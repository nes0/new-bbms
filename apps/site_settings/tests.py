"""Settings the trustees change in the admin (spec 02 §2.8)."""

from django.test import TestCase

from apps.members.models import Member

from .models import SettingChange, SiteSettings

URL = "/admin/site_settings/sitesettings/"


class SiteSettingsTests(TestCase):
    def setUp(self):
        self.admin = Member.objects.create_superuser(
            "admin@example.com", "a-good-password", given_name="A", family_name="D"
        )
        self.client.force_login(self.admin)

    def test_the_minimum_subscription_starts_at_25_pounds(self):
        self.assertEqual(SiteSettings.load().minimum_subscription_pence, 2500)

    def test_there_is_only_one_row(self):
        SiteSettings.load()
        SiteSettings().save()
        self.assertEqual(SiteSettings.objects.count(), 1)

    def test_the_admin_list_goes_straight_to_the_one_row(self):
        self.assertRedirects(self.client.get(URL), f"{URL}1/change/")

    def test_changed_in_pounds_in_the_admin_and_logged(self):
        response = self.client.post(f"{URL}1/change/", {"minimum_subscription_pounds": "30"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SiteSettings.load().minimum_subscription_pence, 3000)
        change = SettingChange.objects.get()
        self.assertEqual((change.old_value, change.new_value, change.changed_by), ("£25.00", "£30.00", self.admin))

    def test_saving_without_a_change_logs_nothing(self):
        self.client.post(f"{URL}1/change/", {"minimum_subscription_pounds": "25"})
        self.assertFalse(SettingChange.objects.exists())

    def test_cannot_be_added_or_deleted(self):
        SiteSettings.load()
        self.assertEqual(self.client.get(f"{URL}add/").status_code, 403)
        self.assertEqual(self.client.get(f"{URL}1/delete/").status_code, 403)

    def test_only_admins_can_change_it(self):
        member = Member.objects.create_user("kim@example.com", "a-good-password", given_name="K", family_name="M")
        self.client.force_login(member)
        self.client.post(f"{URL}1/change/", {"minimum_subscription_pounds": "1"})
        self.assertEqual(SiteSettings.load().minimum_subscription_pence, 2500)
