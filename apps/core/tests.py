from django.test import TestCase
from django.urls import reverse

from apps.members.models import Member


class CorePageTests(TestCase):
    def test_health_check_reports_ok(self):
        response = self.client.get(reverse("healthz"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_home_page_for_visitors(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, "Log in")

    def test_home_page_shows_member_status(self):
        member = Member.objects.create_user("kim@example.com", "a-good-password", given_name="Kim", family_name="K")
        self.client.force_login(member)
        response = self.client.get(reverse("home"))
        self.assertContains(response, "Setting up")

    def test_login_page_and_login(self):
        Member.objects.create_user("kim@example.com", "a-good-password", given_name="Kim", family_name="K")
        self.assertEqual(self.client.get(reverse("login")).status_code, 200)
        response = self.client.post(reverse("login"), {"username": "kim@example.com", "password": "a-good-password"})
        self.assertRedirects(response, reverse("home"))

    def test_admin_requires_staff(self):
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 302)
