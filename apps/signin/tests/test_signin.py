"""Logging in safely (spec 06 §6.11): banned members, rate limits, "remember me" and two-factor login."""

import time

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.equipment.tests.helpers import member
from apps.signin import totp, two_factor
from apps.signin.models import Attempt, TwoFactor
from apps.site_settings.models import SiteSettings

PASSWORD = "a-good-password"
LOGIN = "/accounts/login/"


def log_in(client, who, password=PASSWORD, **extra):
    return client.post(LOGIN, {"username": who.email, "password": password, **extra})


def current_code(who):
    return totp.code_at(TwoFactor.objects.get(member=who).secret, totp.current_step())


def turn_on(who):
    device = two_factor.start(who)
    return two_factor.confirm(who, totp.code_at(device.secret, totp.current_step() - 1))


class TotpTests(TestCase):
    SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238's test key, "12345678901234567890"

    def test_rfc_6238_values(self):
        self.assertEqual(totp.code_at(self.SECRET, 59 // 30), "287082")
        self.assertEqual(totp.code_at(self.SECRET, 1111111109 // 30), "081804")

    def test_a_code_is_good_for_a_step_either_side_and_only_once(self):
        now = 1111111109
        step = totp.current_step(now)
        self.assertEqual(totp.check(self.SECRET, "081804", now=now), step)
        self.assertEqual(totp.check(self.SECRET, "081 804", now=now + 30), step)  # spaces allowed, a little late
        self.assertIsNone(totp.check(self.SECRET, "081804", last_step=step, now=now))  # used already
        self.assertIsNone(totp.check(self.SECRET, "081804", now=now + 120))  # too late
        self.assertIsNone(totp.check(self.SECRET, "abc", now=now))


class LoginTests(TestCase):
    def setUp(self):
        self.kim = member()

    def test_logging_in_and_remember_me(self):
        response = log_in(self.client, self.kim, remember="on")
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertFalse(self.client.session.get_expire_at_browser_close())
        self.client.logout()
        log_in(self.client, self.kim)  # not ticked: logged out when the browser closes
        self.assertTrue(self.client.session.get_expire_at_browser_close())

    def test_a_wrong_password_says_so(self):
        self.assertContains(log_in(self.client, self.kim, "wrong"), "didn&#x27;t match")

    def test_a_banned_member_is_refused_with_a_clear_message(self):
        self.kim.banned = True
        self.kim.save()
        response = log_in(self.client, self.kim)
        self.assertContains(response, "This account has been banned, so you can&#x27;t log in.")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_with_the_wrong_password_a_banned_account_looks_like_any_other(self):
        self.kim.banned = True
        self.kim.save()
        response = log_in(self.client, self.kim, "wrong")
        self.assertNotContains(response, "banned")
        self.assertContains(response, "didn&#x27;t match")

    def test_a_member_banned_while_logged_in_is_logged_out(self):
        self.client.force_login(self.kim)
        self.kim.banned = True
        self.kim.save()
        response = self.client.get("/account/", follow=True)
        self.assertEqual(response.redirect_chain[-1][0], LOGIN)
        self.assertContains(response, "This account has been banned")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_the_admin_sites_own_login_page_isnt_used(self):
        response = self.client.get("/admin/login/?next=/admin/members/")
        self.assertEqual(response["Location"], f"{LOGIN}?next=/admin/members/")


class RateLimitTests(TestCase):
    def setUp(self):
        self.kim = member()

    def test_five_wrong_passwords_lock_that_address_for_a_while(self):
        for _ in range(5):
            log_in(self.client, self.kim, "wrong")
        response = log_in(self.client, self.kim)
        self.assertContains(response, "Too many tries. Please wait 15 minutes")
        self.assertNotIn("_auth_user_id", self.client.session)
        other = member()
        log_in(self.client, other)  # someone else isn't affected
        self.assertEqual(int(self.client.session["_auth_user_id"]), other.pk)

    def test_the_same_limit_whether_or_not_there_is_an_account(self):
        for _ in range(5):
            self.client.post(LOGIN, {"username": "nobody@example.com", "password": "x"})
        response = self.client.post(LOGIN, {"username": "nobody@example.com", "password": "x"})
        self.assertContains(response, "Too many tries")

    def test_logging_in_clears_the_count(self):
        for _ in range(4):
            log_in(self.client, self.kim, "wrong")
        log_in(self.client, self.kim)
        self.client.logout()
        for _ in range(4):
            log_in(self.client, self.kim, "wrong")
        log_in(self.client, self.kim)
        self.assertIn("_auth_user_id", self.client.session)

    def test_attempts_store_a_hash_not_the_address(self):
        log_in(self.client, self.kim, "wrong")
        self.assertNotIn(self.kim.email, Attempt.objects.get().key)

    @override_settings(CLIENT_IP_HEADER="HTTP_X_VISITOR")
    def test_one_visitor_trying_many_addresses_is_limited_once_the_proxy_header_is_set(self):
        for n in range(30):
            self.client.post(
                LOGIN, {"username": f"guess{n}@example.com", "password": "x"}, HTTP_X_VISITOR="203.0.113.9"
            )
        response = self.client.post(
            LOGIN, {"username": self.kim.email, "password": PASSWORD}, HTTP_X_VISITOR="203.0.113.9"
        )
        self.assertContains(response, "Too many tries")
        log_in(self.client, self.kim, HTTP_X_VISITOR="198.51.100.1")  # from somewhere else: fine
        self.assertIn("_auth_user_id", self.client.session)

    def test_without_the_header_there_is_no_limit_by_address(self):
        for n in range(30):
            self.client.post(LOGIN, {"username": f"guess{n}@example.com", "password": "x"})
        log_in(self.client, self.kim)
        self.assertIn("_auth_user_id", self.client.session)

    def test_password_reset_emails_are_limited(self):
        for _ in range(4):
            response = self.client.post("/accounts/password_reset/", {"email": self.kim.email})
        self.assertEqual(len(mail.outbox), 3)
        self.assertContains(response, "We&#x27;ve sent several reset emails already")


class TwoFactorSetupTests(TestCase):
    def setUp(self):
        self.kim = member()
        self.client.force_login(self.kim)
        self.url = reverse("signin:manage")

    def test_setting_it_up_shows_recovery_codes_once(self):
        response = self.client.get(self.url)
        self.assertContains(response, "<svg")
        self.assertContains(response, TwoFactor.objects.get(member=self.kim).secret)
        self.assertContains(self.client.post(self.url, {"code": "000000"}), "That code isn")
        self.assertFalse(two_factor.is_on(self.kim))
        response = self.client.post(self.url, {"code": current_code(self.kim)})
        self.assertContains(response, "Save these recovery codes")
        self.assertEqual(len(response.context["codes"]), 10)
        self.assertTrue(two_factor.is_on(self.kim))
        self.assertContains(self.client.get(self.url), "You have 10 recovery codes left.")

    def test_the_account_page_links_here(self):
        self.assertContains(self.client.get("/account/"), self.url)

    def test_turning_off_and_new_codes_need_the_password(self):
        old = turn_on(self.kim)
        self.client.post(reverse("signin:new-codes"), {"password": "wrong"})
        response = self.client.post(reverse("signin:new-codes"), {"password": PASSWORD})
        self.assertEqual(len(response.context["codes"]), 10)
        self.assertEqual(two_factor.verify(self.kim, old[0]), "")  # old codes stop working
        self.client.post(reverse("signin:turn-off"), {"password": "wrong"})
        self.assertTrue(two_factor.is_on(self.kim))
        self.client.post(reverse("signin:turn-off"), {"password": PASSWORD})
        self.assertFalse(two_factor.is_on(self.kim))


class TwoFactorLoginTests(TestCase):
    def setUp(self):
        self.kim = member()
        self.codes = turn_on(self.kim)

    def test_the_password_alone_isnt_enough(self):
        response = log_in(self.client, self.kim)
        self.assertRedirects(response, reverse("signin:two-factor"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.get("/account/").status_code, 302)

    def test_the_code_logs_you_in_and_goes_where_you_were_going(self):
        self.client.post(LOGIN, {"username": self.kim.email, "password": PASSWORD, "next": "/equipment/"})
        response = self.client.post(reverse("signin:two-factor"), {"code": current_code(self.kim)})
        self.assertEqual(response["Location"], "/equipment/")
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.kim.pk)

    def test_a_code_cant_be_used_twice(self):
        code = current_code(self.kim)
        log_in(self.client, self.kim)
        self.client.post(reverse("signin:two-factor"), {"code": code})
        self.client.logout()
        log_in(self.client, self.kim)
        response = self.client.post(reverse("signin:two-factor"), {"code": code})
        self.assertContains(response, "That code isn")

    def test_a_recovery_code_works_once(self):
        log_in(self.client, self.kim)
        response = self.client.post(reverse("signin:two-factor"), {"code": self.codes[0].lower()}, follow=True)
        self.assertContains(response, "You used a recovery code; 9 left.")
        self.client.logout()
        log_in(self.client, self.kim)
        self.assertContains(self.client.post(reverse("signin:two-factor"), {"code": self.codes[0]}), "That code isn")

    def test_wrong_codes_are_limited(self):
        log_in(self.client, self.kim)
        for _ in range(5):
            self.client.post(reverse("signin:two-factor"), {"code": "000000"})
        response = self.client.post(reverse("signin:two-factor"), {"code": current_code(self.kim)}, follow=True)
        self.assertContains(response, "Too many wrong codes")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_the_code_must_come_soon_after_the_password(self):
        log_in(self.client, self.kim)
        session = self.client.session
        session["signin.pending"]["until"] = time.time() - 1
        session.save()
        response = self.client.post(reverse("signin:two-factor"), {"code": current_code(self.kim)}, follow=True)
        self.assertContains(response, "That took too long")
        self.assertNotIn("_auth_user_id", self.client.session)


class AdminSiteTests(TestCase):
    def setUp(self):
        self.boss = member(is_staff=True, is_superuser=True)

    def test_an_admin_with_two_factor_gives_a_code_before_the_admin_site(self):
        turn_on(self.boss)
        self.client.force_login(self.boss)  # e.g. a session from before it was turned on
        response = self.client.get("/admin/")
        self.assertEqual(response["Location"], f"{reverse('signin:two-factor')}?next=/admin/")
        self.client.post(reverse("signin:two-factor"), {"code": current_code(self.boss), "next": "/admin/"})
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_the_setting_makes_admins_set_it_up(self):
        self.client.force_login(self.boss)
        self.assertEqual(self.client.get("/admin/").status_code, 200)  # off to start with
        row = SiteSettings.load()
        row.admins_need_two_factor = True
        row.save()
        self.assertEqual(self.client.get("/admin/")["Location"], reverse("signin:manage"))
        self.client.get(reverse("signin:manage"))
        self.client.post(reverse("signin:manage"), {"code": current_code(self.boss)})
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_an_admin_can_switch_off_a_members_two_factor(self):
        kim = member()
        turn_on(kim)
        self.client.force_login(self.boss)
        url = reverse("admin:signin_twofactor_changelist")
        device = TwoFactor.objects.get(member=kim)
        self.client.post(url, {"action": "switch_off", "_selected_action": [device.pk]})
        self.assertFalse(two_factor.is_on(kim))
        self.assertEqual(two_factor.recovery_codes_left(kim), 0)


class DeployCheckTests(TestCase):
    def test_a_missing_ip_header_is_warned_about(self):
        from apps.signin.checks import client_ip_header_is_set

        self.assertEqual([w.id for w in client_ip_header_is_set(None)], ["signin.W001"])
        with override_settings(CLIENT_IP_HEADER="HTTP_DO_CONNECTING_IP"):
            self.assertEqual(client_ip_header_is_set(None), [])
