"""Log in with BBMS (spec 06 §6.12): the whole OpenID Connect flow, as Moodle would drive it."""

import io
from io import StringIO
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from jwcrypto import jwt
from jwcrypto.jwk import JWK

from apps.members.models import Member

from .helpers import set_moodle_address

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
).decode()
CALLBACK = "https://moodle.example.com/admin/oauth2callback.php"


def provider_settings(key=PEM):
    return {**settings.OAUTH2_PROVIDER, "OIDC_ENABLED": bool(key), "OIDC_RSA_PRIVATE_KEY": key}


def make_client():
    """Register Moodle with the setup command, returning (client id, secret) from what it prints."""
    out = StringIO()
    call_command("setup_moodle_sso", stdout=out)
    text = out.getvalue()
    client_id = text.split("Client ID:")[1].split()[0]
    secret = text.split("Client secret:")[1].split()[0]
    return client_id, secret


@override_settings(OAUTH2_PROVIDER=provider_settings(), OIDC_RSA_PRIVATE_KEY=PEM)
class LogInWithBBMSTests(TestCase):
    def setUp(self):
        set_moodle_address()
        self.client_id, self.secret = make_client()
        self.kim = Member.objects.create_user(
            "kim@example.com",
            "a-good-password",
            given_name="Kim",
            family_name="Maker",
            email_verified_at=timezone.now(),
        )

    def authorize(self, **extra):
        query = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": CALLBACK,
            "scope": "openid profile email",
            "state": "abc123",
            **extra,
        }
        return self.client.get("/o/authorize/", query)

    def code_from(self, response):
        self.assertEqual(response.status_code, 302, getattr(response, "content", b"")[:300])
        location = urlparse(response["Location"])
        self.assertEqual(f"{location.scheme}://{location.netloc}{location.path}", CALLBACK)
        query = parse_qs(location.query)
        self.assertEqual(query["state"], ["abc123"])
        return query["code"][0]

    def exchange(self, code, secret=None):
        return self.client.post(
            "/o/token/",
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CALLBACK,
                "client_id": self.client_id,
                "client_secret": secret or self.secret,
            },
        )

    def test_a_logged_out_member_is_sent_to_the_login_page_and_back(self):
        response = self.authorize()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/accounts/login/?next=/o/authorize/"))

    def test_the_whole_flow_gives_moodle_who_the_member_is(self):
        self.client.force_login(self.kim)
        code = self.code_from(self.authorize())  # no "do you allow?" page for Moodle
        response = self.exchange(code)
        self.assertEqual(response.status_code, 200, response.content)
        tokens = response.json()
        # The ID token is signed with BBMS's key and says who they are.
        public = JWK.from_pem(
            KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        )
        claims = jwt.JWT(jwt=tokens["id_token"], key=public).claims
        self.assertIn(f'"sub":"{self.kim.pk}"', claims.replace(" ", ""))
        # So does the userinfo endpoint, which Moodle reads the details from.
        info = self.client.get("/o/userinfo/", headers={"Authorization": f"Bearer {tokens['access_token']}"}).json()
        self.assertEqual(
            info,
            {
                "sub": str(self.kim.pk),
                "email": "kim@example.com",
                "email_verified": True,
                "given_name": "Kim",
                "family_name": "Maker",
                "name": self.kim.get_full_name(),
            },
        )

    def test_an_unconfirmed_email_address_is_sent_to_confirm_it_first(self):
        self.kim.email_verified_at = None
        self.kim.save()
        self.client.force_login(self.kim)
        response = self.authorize()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Confirm your email address first")
        self.assertContains(response, "kim@example.com")
        self.assertNotIn("code=", response.get("Location", ""))

    def test_the_email_is_only_ever_claimed_verified_once_it_is(self):
        self.client.force_login(self.kim)
        tokens = self.exchange(self.code_from(self.authorize())).json()
        info = self.client.get("/o/userinfo/", headers={"Authorization": f"Bearer {tokens['access_token']}"}).json()
        self.assertIs(info["email_verified"], True)

    def test_a_member_still_setting_up_may_log_in(self):
        self.assertEqual(self.kim.status, Member.Status.SETTING_UP)
        self.client.force_login(self.kim)
        self.code_from(self.authorize())

    def test_a_banned_member_may_not(self):
        self.kim.banned = True
        self.kim.save()
        self.client.force_login(self.kim)
        response = self.authorize()
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "You can't log in to Moodle", status_code=403)
        self.assertNotIn("code=", response.get("Location", ""))

    def test_a_deactivated_login_cant_even_reach_it(self):
        self.kim.is_active = False
        self.kim.save()
        self.assertFalse(self.client.login(username="kim@example.com", password="a-good-password"))

    def test_the_wrong_secret_is_refused(self):
        self.client.force_login(self.kim)
        response = self.exchange(self.code_from(self.authorize()), secret="not-the-secret")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("access_token", response.content.decode())

    def test_a_code_works_once(self):
        self.client.force_login(self.kim)
        code = self.code_from(self.authorize())
        self.assertEqual(self.exchange(code).status_code, 200)
        self.assertEqual(self.exchange(code).status_code, 400)

    def test_only_the_registered_callback_address_is_accepted(self):
        self.client.force_login(self.kim)
        response = self.authorize(redirect_uri="https://evil.example.com/steal")
        self.assertNotEqual(response.status_code, 302)
        self.assertNotIn("evil.example.com", response.get("Location", ""))

    def test_the_discovery_document_lists_only_the_endpoints_moodle_uses(self):
        document = self.client.get("/o/.well-known/openid-configuration").json()
        site = settings.SITE_URL
        self.assertEqual(document["issuer"], f"{site}/o")
        self.assertEqual(document["authorization_endpoint"], f"{site}/o/authorize/")
        self.assertEqual(document["token_endpoint"], f"{site}/o/token/")
        self.assertEqual(document["userinfo_endpoint"], f"{site}/o/userinfo/")
        self.assertEqual(self.client.get("/o/.well-known/jwks.json").status_code, 200)
        self.assertIn("RS256", document["id_token_signing_alg_values_supported"])

    def test_members_cant_register_their_own_applications(self):
        self.client.force_login(self.kim)
        for path in ("/o/applications/", "/o/applications/register/", "/o/authorized_tokens/", "/o/introspect/"):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_the_provider_is_off_until_a_signing_key_is_set(self):
        with override_settings(OAUTH2_PROVIDER=provider_settings(key=""), OIDC_RSA_PRIVATE_KEY=""):
            self.client.force_login(self.kim)
            self.assertEqual(self.authorize().status_code, 404)


@override_settings(OAUTH2_PROVIDER=provider_settings(), OIDC_RSA_PRIVATE_KEY=PEM)
class SetupCommandTests(TestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command("setup_moodle_sso", *args, stdout=out)
        return out.getvalue()

    def test_it_needs_moodles_address_first(self):
        with self.assertRaisesMessage(CommandError, "Set Moodle's address first"):
            call_command("setup_moodle_sso", stdout=io.StringIO())

    @override_settings(OIDC_RSA_PRIVATE_KEY="")
    def test_it_needs_the_signing_key(self):
        set_moodle_address()
        with self.assertRaisesMessage(CommandError, "OIDC_RSA_PRIVATE_KEY"):
            call_command("setup_moodle_sso", stdout=io.StringIO())

    def test_it_prints_what_moodle_needs_and_shows_the_secret_only_once(self):
        set_moodle_address("https://moodle.example.com/")
        first = self.run_command()
        self.assertIn(f"{settings.SITE_URL}/o/authorize/", first)
        self.assertIn(CALLBACK, first)
        self.assertIn("shown once", first)
        again = self.run_command()
        self.assertIn("unchanged", again)
        self.assertNotIn(first.split("Client secret:")[1].split()[0], again)
        rotated = self.run_command("--new-secret")
        self.assertNotEqual(first.split("Client secret:")[1].split()[0], rotated.split("Client secret:")[1].split()[0])

    def test_changing_moodles_address_updates_the_callback_but_keeps_one_application(self):
        from oauth2_provider.models import get_application_model

        set_moodle_address("https://moodle.example.com")
        self.run_command()
        set_moodle_address("https://learn.example.org")
        self.run_command()
        (app,) = get_application_model().objects.all()
        self.assertEqual(app.redirect_uris, "https://learn.example.org/admin/oauth2callback.php")
        self.assertTrue(app.skip_authorization)
