"""Confirming and changing the email address (spec 03 §3.1)."""

import re
from datetime import timedelta
from unittest import mock
from urllib.parse import urlparse

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from . import email_confirmation
from .models import Member

PASSWORD = "a-good-password"


def person(email="kim@example.com", **fields):
    defaults = {"given_name": "Kim", "family_name": "Maker", "phone": "01273 123456", "emergency_contact": "Sam"}
    return Member.objects.create_user(email, PASSWORD, **{**defaults, **fields})


def link_in(message):
    """The confirmation link in an email, as a path."""
    return re.search(r"https?://[^/\s]+(/account/confirm-email/[^\s]+)", message.body).group(1)


class RegistrationTests(TestCase):
    def setUp(self):
        cache.clear()

    def register(self):
        from .test_registration import details

        return self.client.post("/register", details(email="Kim@Example.com"))

    def test_joining_sends_a_link_and_following_it_confirms_the_address(self):
        self.register()
        member = Member.objects.get(email="kim@example.com")
        self.assertFalse(member.email_verified)
        (message,) = [m for m in mail.outbox if "confirm" in m.subject.lower()]
        self.assertEqual(message.to, ["kim@example.com"])
        response = self.client.get(link_in(message))  # not logged in: the link is the proof
        self.assertContains(response, "your email address is confirmed")
        member.refresh_from_db()
        self.assertTrue(member.email_verified)

    def test_following_the_link_twice_is_fine(self):
        self.register()
        (message,) = [m for m in mail.outbox if "confirm" in m.subject.lower()]
        self.client.get(link_in(message))
        self.assertContains(self.client.get(link_in(message)), "already confirmed")

    @override_settings(EMAIL_BACKEND="apps.core.test_email.BrokenMailServer")
    def test_a_broken_mail_server_doesnt_stop_anyone_joining(self):
        response = self.register()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Member.objects.filter(email="kim@example.com").exists())


class LinkTests(TestCase):
    def setUp(self):
        self.kim = person()

    def token(self, kind="verify", email=None, member=None):
        member = member or self.kim
        path = urlparse(email_confirmation._link(kind, member, email or member.email)).path
        return path.strip("/").split("/")[-1]

    def test_a_made_up_or_damaged_link_does_nothing(self):
        for token in ("nonsense", "a:b:c", "x" * 80):
            self.assertEqual(email_confirmation.confirm(token), (email_confirmation.INVALID, None))
        good = self.token()
        self.assertEqual(email_confirmation.confirm(good + "x")[0], email_confirmation.INVALID)
        self.kim.refresh_from_db()
        self.assertFalse(self.kim.email_verified)

    def test_links_last_a_week(self):
        token = self.token()
        with mock.patch("django.core.signing.time.time", return_value=timezone.now().timestamp() + 6 * 86400):
            self.assertEqual(email_confirmation.confirm(token)[0], email_confirmation.CONFIRMED)
        self.kim.email_verified_at = None
        self.kim.save()
        with mock.patch("django.core.signing.time.time", return_value=timezone.now().timestamp() + 8 * 86400):
            self.assertEqual(email_confirmation.confirm(token)[0], email_confirmation.INVALID)

    def test_a_link_for_an_address_the_member_no_longer_has_does_nothing(self):
        token = self.token()
        self.kim.email = "new@example.com"
        self.kim.save()
        self.assertEqual(email_confirmation.confirm(token)[0], email_confirmation.INVALID)
        self.kim.refresh_from_db()
        self.assertFalse(self.kim.email_verified)

    def test_a_deleted_member_s_link_does_nothing(self):
        token = self.token()
        self.kim.delete()
        self.assertEqual(email_confirmation.confirm(token), (email_confirmation.INVALID, None))

    def test_a_link_is_for_one_member(self):
        sam = person("sam@example.com")
        token = self.token(member=sam)
        email_confirmation.confirm(token)
        self.kim.refresh_from_db()
        self.assertFalse(self.kim.email_verified)
        self.assertTrue(Member.objects.get(pk=sam.pk).email_verified)

    def test_a_change_link_cant_be_used_to_confirm_the_old_address(self):
        """A "verify" token for the address they have, versus a "change" token for another: never swapped."""
        token = self.token("change", "new@example.com")
        outcome, _ = email_confirmation.confirm(token)
        self.assertEqual(outcome, email_confirmation.CHANGED)


class ResendTests(TestCase):
    def setUp(self):
        cache.clear()
        self.kim = person()
        self.client.force_login(self.kim)

    def test_the_account_page_shows_it_isnt_confirmed_and_offers_a_new_link(self):
        page = self.client.get("/account/")
        self.assertContains(page, "not confirmed")
        self.assertContains(page, "Email me a link to confirm it")

    def test_a_new_link_is_sent_but_not_more_than_one_a_minute(self):
        self.client.post(reverse("members:resend-confirmation"))
        self.assertEqual(len(mail.outbox), 1)
        response = self.client.post(reverse("members:resend-confirmation"), follow=True)
        self.assertEqual(len(mail.outbox), 1)
        self.assertContains(response, "only just sent one")

    def test_a_confirmed_member_isnt_emailed(self):
        self.kim.email_verified_at = timezone.now()
        self.kim.save()
        response = self.client.post(reverse("members:resend-confirmation"), follow=True)
        self.assertEqual(mail.outbox, [])
        self.assertContains(response, "already confirmed")
        self.assertContains(self.client.get("/account/"), "✓ confirmed")

    def test_it_needs_a_post_and_a_login(self):
        self.assertEqual(self.client.get(reverse("members:resend-confirmation")).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.post(reverse("members:resend-confirmation")).status_code, 302)

    def test_a_setting_up_member_sees_it_as_a_next_step(self):
        self.assertContains(self.client.get("/account/"), "Confirm your email address")


class ChangeEmailTests(TestCase):
    def setUp(self):
        cache.clear()
        self.kim = person(email_verified_at=timezone.now())
        self.sam = person("sam@example.com")
        self.client.force_login(self.kim)
        self.url = reverse("members:change-email")

    def ask(self, new="New@Example.com", password=PASSWORD):
        return self.client.post(self.url, {"new_email": new, "password": password})

    def test_a_link_goes_to_the_new_address_and_nothing_changes_yet(self):
        self.assertRedirects(self.ask(), "/account/")
        to_new = [m for m in mail.outbox if m.to == ["new@example.com"]]
        to_old = [m for m in mail.outbox if m.to == ["kim@example.com"]]
        self.assertEqual((len(to_new), len(to_old)), (1, 1))
        self.assertIn("link", to_new[0].body)
        self.assertNotIn("/confirm-email/", to_old[0].body)  # the old address is warned, not given the link
        self.kim.refresh_from_db()
        self.assertEqual((self.kim.email, self.kim.email_verified), ("kim@example.com", True))

    def test_following_the_link_changes_the_login_and_warns_the_old_address(self):
        self.ask()
        link = link_in([m for m in mail.outbox if m.to == ["new@example.com"]][0])
        self.client.logout()
        response = self.client.get(link)  # works without being logged in
        self.assertContains(response, "you now log in with")
        self.kim.refresh_from_db()
        self.assertEqual((self.kim.email, self.kim.email_verified), ("new@example.com", True))
        self.assertTrue(self.client.login(username="new@example.com", password=PASSWORD))
        self.client.logout()
        self.assertFalse(self.client.login(username="kim@example.com", password=PASSWORD))
        warning = [m for m in mail.outbox if m.to == ["kim@example.com"] and "was changed" in m.subject]
        self.assertEqual(len(warning), 1)
        self.assertIn("new@example.com", warning[0].body)

    def test_the_wrong_password_sends_nothing(self):
        response = self.ask(password="not-my-password")
        self.assertContains(response, "That isn")
        self.assertEqual(mail.outbox, [])

    def test_an_address_already_in_use_is_refused_whatever_its_case(self):
        self.assertContains(self.ask(new="SAM@example.com"), "already an account")
        self.assertEqual(mail.outbox, [])

    def test_the_same_address_is_refused(self):
        self.assertContains(self.ask(new="Kim@Example.com"), "already have")

    def test_an_address_taken_before_the_link_is_followed_is_refused(self):
        self.ask()
        link = link_in([m for m in mail.outbox if m.to == ["new@example.com"]][0])
        person("new@example.com")
        response = self.client.get(link)
        self.assertContains(response, "Another account is now using that address", status_code=400)
        self.kim.refresh_from_db()
        self.assertEqual(self.kim.email, "kim@example.com")

    def test_not_more_than_one_request_a_minute(self):
        self.ask()
        mail.outbox.clear()
        response = self.ask(new="other@example.com")
        self.assertEqual(mail.outbox, [])
        self.assertContains(response, "only just sent one")

    def test_you_must_be_logged_in(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_the_details_page_points_here(self):
        self.assertContains(self.client.get("/account/details/"), escape(self.url))


class AdminTests(TestCase):
    def setUp(self):
        self.admin = Member.objects.create_superuser("admin@example.com", PASSWORD, given_name="A", family_name="D")
        self.client.force_login(self.admin)
        self.kim = person(email_verified_at=timezone.now() - timedelta(days=3))

    def test_the_list_shows_and_filters_who_has_confirmed(self):
        sam = person("sam@example.com")
        url = reverse("admin:members_member_changelist")
        self.assertContains(self.client.get(url + "?email=unconfirmed"), sam.email)
        self.assertNotContains(self.client.get(url + "?email=unconfirmed"), self.kim.email)
        self.assertContains(self.client.get(url + "?email=confirmed"), self.kim.email)

    def test_changing_a_members_email_in_the_admin_clears_the_confirmation(self):
        from django.contrib.admin.sites import site

        from .admin import MemberAdmin

        model_admin = MemberAdmin(Member, site)
        self.kim.email = "changed@example.com"
        form = mock.Mock(changed_data=["email"])
        request = mock.Mock(user=self.admin)
        model_admin.save_model(request, self.kim, form, change=True)
        self.kim.refresh_from_db()
        self.assertEqual((self.kim.email, self.kim.email_verified), ("changed@example.com", False))

    def test_changing_something_else_keeps_it(self):
        from django.contrib.admin.sites import site

        from .admin import MemberAdmin

        self.kim.phone = "01273 999999"
        MemberAdmin(Member, site).save_model(
            mock.Mock(user=self.admin), self.kim, mock.Mock(changed_data=["phone"]), True
        )
        self.kim.refresh_from_db()
        self.assertTrue(self.kim.email_verified)
