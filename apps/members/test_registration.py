"""Joining online (spec 03 §3.1)."""

from django.test import TestCase

from .forms import normalise_postcode
from .models import Address, Member


def details(**changes):
    data = {
        "given_name": "Kim",
        "family_name": "Maker",
        "email": "Kim.Maker@Example.com",
        "password": "solder-and-sawdust",
        "phone": "01273 123456",
        "address_line_1": "1 Example Street",
        "address_line_2": "",
        "address_line_3": "",
        "address_line_4": "",
        "postcode": "bn1 4aa",
        "monthly_subscription": "25",
        "emergency_contact": "Sam Maker, 07700 900123",
        "rules_agreed": "on",
        "visited_space": "on",
    }
    data.update(changes)
    return {key: value for key, value in data.items() if value is not None}


class RegistrationTests(TestCase):
    def setUp(self):
        self.client = self.client_class(enforce_csrf_checks=False)

    def register(self, **changes):
        return self.client.post("/register", details(**changes))

    def test_the_form_is_public(self):
        response = self.client.get("/register")
        self.assertContains(response, "Join Build Brighton")
        self.assertContains(response, 'name="postcode"')

    def test_joining_creates_a_setting_up_member_and_logs_them_in(self):
        response = self.register()
        self.assertRedirects(response, "/account/")
        member = Member.objects.get()
        self.assertEqual(member.email, "kim.maker@example.com")
        self.assertEqual(member.status, Member.Status.SETTING_UP)
        self.assertFalse(member.has_space_access)
        self.assertEqual(member.monthly_subscription_pence, 2500)
        self.assertIsNotNone(member.rules_agreed_at)
        self.assertTrue(member.check_password("solder-and-sawdust"))
        account = self.client.get("/account/")
        self.assertContains(account, "Next steps")
        self.assertContains(account, "£25 a month")

    def test_the_address_waits_for_approval(self):
        self.register()
        address = Address.objects.get()
        self.assertEqual((address.line_1, address.postcode, address.approved), ("1 Example Street", "BN1 4AA", False))

    def test_the_subscription_must_be_at_least_20_pounds_in_whole_pounds(self):
        self.assertContains(self.register(monthly_subscription="19"), "greater than or equal to 20")
        self.assertContains(self.register(monthly_subscription="20.50"), "Enter a whole number")
        self.assertFalse(Member.objects.exists())

    def test_both_boxes_must_be_ticked(self):
        response = self.register(rules_agreed=None, visited_space=None)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].errors.keys(), {"rules_agreed", "visited_space"})
        self.assertFalse(Member.objects.exists())

    def test_required_fields(self):
        response = self.register(phone="", emergency_contact="", address_line_1="", postcode="")
        self.assertEqual(
            set(response.context["form"].errors), {"phone", "emergency_contact", "address_line_1", "postcode"}
        )

    def test_phone_needs_at_least_10_characters(self):
        self.assertIn("phone", self.register(phone="12345").context["form"].errors)

    def test_a_used_email_is_refused_whatever_its_case(self):
        Member.objects.create_user("kim.maker@example.com", "another-password", given_name="K", family_name="M")
        response = self.register()
        self.assertContains(response, "already an account with that email address")
        self.assertEqual(Member.objects.count(), 1)

    def test_weak_passwords_are_refused(self):
        for weak in ("short", "password123", "12345678901", "kimmaker"):
            response = self.register(password=weak)
            self.assertIn("password", response.context["form"].errors, weak)
        self.assertFalse(Member.objects.exists())

    def test_the_password_is_not_sent_back_after_an_error(self):
        response = self.register(postcode="nope")
        self.assertNotContains(response, "solder-and-sawdust")

    def test_logged_in_members_go_to_their_account(self):
        self.register()
        self.assertRedirects(self.client.get("/register"), "/account/")

    def test_the_account_page_needs_a_login(self):
        response = self.client.get("/account/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_csrf_is_enforced(self):
        client = self.client_class(enforce_csrf_checks=True)
        self.assertEqual(client.post("/register", details()).status_code, 403)


class PostcodeTests(TestCase):
    def test_valid_postcodes_are_tidied(self):
        for raw, tidy in [
            ("bn1 4aa", "BN1 4AA"),
            ("BN14AA", "BN1 4AA"),
            ("sw1a1aa", "SW1A 1AA"),
            (" M1  1AE ", "M1 1AE"),
            ("EC1A 1BB", "EC1A 1BB"),
            ("gir 0aa", "GIR 0AA"),
        ]:
            self.assertEqual(normalise_postcode(raw), tidy, raw)

    def test_invalid_postcodes_are_rejected(self):
        for raw in ("", "nope", "12345", "BN1", "BN1 4A", "1BN 4AA", "BN1 4AAA"):
            self.assertIsNone(normalise_postcode(raw), raw)
