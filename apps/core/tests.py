from django.contrib.staticfiles import finders
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from apps.core import markup
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


class BrandingTests(TestCase):
    def test_every_page_has_the_logo_linking_home(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, 'src="/static/img/logo.png"')
        self.assertContains(response, f'href="{reverse("home")}"><img class="logo"')

    def test_login_and_join_pages_have_the_backdrop_but_others_do_not(self):
        self.assertContains(self.client.get(reverse("login")), 'class="backdrop"')
        self.assertContains(self.client.get(reverse("members:register")), 'class="backdrop"')
        self.assertNotContains(self.client.get(reverse("home")), "backdrop")

    def test_pages_link_the_favicon(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, 'rel="icon" href="/static/img/favicon.svg"')
        self.assertContains(response, 'rel="apple-touch-icon"')

    def test_the_logo_and_backdrop_files_are_in_the_static_folder(self):
        for name in ("logo.png", "login-backdrop.jpg", "favicon.svg", "favicon.ico", "apple-touch-icon.png"):
            self.assertIsNotNone(finders.find(f"img/{name}"), name)


class MarkupTests(SimpleTestCase):
    """Markdown typed into the site (core/markup.py) can't inject HTML or script links."""

    def test_formatting_works(self):
        html = markup.to_html("# Plan\n\n- **one**\n- [two](https://example.com/x)\n\n<https://example.org>")
        self.assertIn("<strong>one</strong>", html)
        self.assertIn('<a href="https://example.com/x" rel="nofollow noopener">two</a>', html)
        self.assertIn('href="https://example.org"', html)

    def test_html_and_unsafe_links_are_removed(self):
        for text, banned in (
            ("<script>alert(1)</script>", "<script"),
            ("<div onclick=x>hi</div>", "<div"),
            ("a <img src=x onerror=alert(1)> b", "<img"),
            ("[x](javascript:alert(1))", "javascript:"),
            ("[x](JaVa\nScRiPt:alert(1))", "alert"),
            ("![x](data:text/html,hi)", "data:"),
            ("[x](//evil.example/)", "evil"),
        ):
            self.assertNotIn(banned, markup.to_html(text).lower(), text)


class HeaderLinkTests(TestCase):
    """Every link in the header and footer opens for whoever is shown it (no "Not found" for someone setting up)."""

    def links(self, member):
        import re

        self.client.force_login(member)
        html = self.client.get(reverse("home")).content.decode()
        nav = re.search(r"<nav>(.*?)</nav>", html, re.S).group(1) + re.search(r"<footer.*?</footer>", html, re.S)[0]
        return re.findall(r'href="([^"]+)"', nav)

    def test_links_work_for_each_kind_of_member(self):
        from apps.equipment.tests.helpers import member as make_member

        for status, members_pages in (
            (Member.Status.SETTING_UP, False),
            (Member.Status.ACTIVE, True),
            (Member.Status.SUSPENDED, True),  # as legacy: suspended members still see the members' pages
        ):
            with self.subTest(status=status):
                links = self.links(make_member(status=status))
                self.assertEqual(reverse("equipment:index") in links, members_pages)
                self.assertEqual(reverse("feedback") in links, members_pages)
                for link in links:
                    self.assertLess(self.client.get(link).status_code, 400, link)

    def test_public_directory_is_offered_to_someone_setting_up(self):
        from apps.equipment.tests.helpers import member as make_member
        from apps.site_settings.models import SiteSettings

        config = SiteSettings.load()
        config.public_member_directory = True
        config.save()
        links = self.links(make_member(status=Member.Status.SETTING_UP))
        self.assertIn(reverse("profiles:directory"), links)
        self.assertNotIn(reverse("proposals:index"), links)
