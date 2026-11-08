"""
Register Moodle as the one client of "Log in with BBMS" (spec 06 §6.12) and print what to enter in Moodle.

Run it once after setting Moodle's address (admin Settings → Moodle) and OIDC_RSA_PRIVATE_KEY. Running it again
updates Moodle's callback address but keeps the secret; `--new-secret` makes a new one (and shows it). The secret is
shown only when it's made: BBMS keeps just a hash of it. Put it straight into Moodle, then clear your terminal.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from oauth2_provider.generators import generate_client_secret
from oauth2_provider.models import get_application_model

from apps.moodle import client

NAME = "Moodle"


class Command(BaseCommand):
    help = "Register Moodle as a client of 'Log in with BBMS' and print the details Moodle needs."

    def add_arguments(self, parser):
        parser.add_argument("--new-secret", action="store_true", help="Make a new client secret (and show it).")

    def handle(self, *args, new_secret=False, **options):
        base = client.moodle_url()
        if not base:
            raise CommandError("Set Moodle's address first (admin → Settings → Moodle).")
        if not settings.OIDC_RSA_PRIVATE_KEY:
            raise CommandError("Set OIDC_RSA_PRIVATE_KEY first (see docs/deployment.md, Moodle).")
        Application = get_application_model()  # noqa: N806 - a class
        callback = f"{base}/admin/oauth2callback.php"
        app = Application.objects.filter(name=NAME).first()
        secret = None
        if app is None:
            secret = generate_client_secret()
            app = Application(name=NAME, client_secret=secret)
        elif new_secret:
            secret = generate_client_secret()
            app.client_secret = secret
        app.client_type = Application.CLIENT_CONFIDENTIAL
        app.authorization_grant_type = Application.GRANT_AUTHORIZATION_CODE
        app.algorithm = Application.RS256_ALGORITHM
        app.redirect_uris = callback
        app.skip_authorization = True  # members don't need asking whether Moodle may see their name
        app.save()

        site = settings.SITE_URL
        out = self.stdout.write
        out("Enter these in Moodle (Site administration → Server → OAuth 2 services):")
        out(f"  Callback address (Moodle's own, already used): {callback}")
        out(f"  Discovery address:      {site}/o/.well-known/openid-configuration")
        out(f"  Authorization endpoint: {site}/o/authorize/")
        out(f"  Token endpoint:         {site}/o/token/")
        out(f"  Userinfo endpoint:      {site}/o/userinfo/")
        out("  Scopes:                 openid profile email")
        out(f"  Client ID:              {app.client_id}")
        if secret:
            out(f"  Client secret:          {secret}   (shown once; BBMS keeps only a hash)")
        else:
            out("  Client secret:          unchanged (use --new-secret to make another)")
