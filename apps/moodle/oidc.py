"""
"Log in with BBMS" (spec 06 §6.12): BBMS is an OpenID Connect provider, and Moodle is its one client.

Passwords stay in BBMS. Moodle sends the member here, the member logs in as usual, and Moodle is told who they are:
- `sub`: their BBMS id, which never changes (Moodle keeps it as the user's ID number, so BBMS can find them later);
- `email`, `given_name`, `family_name` and `name`;
- `email_verified`: always true, because a member must have confirmed their email address (03 §3.1) before they can
  sign in to Moodle. That's what makes it safe for Moodle to match a login to an existing Moodle account by email.

Anyone with a working login who isn't banned may sign in to Moodle, including members still setting up, so they can
do online training before or soon after joining (decided 2026-11-07, 01 §1.7 #15) - once they've confirmed their
email address, which the page they're sent to explains.
"""

from django.http import Http404
from django.shortcuts import render
from oauth2_provider.oauth2_validators import OAuth2Validator
from oauth2_provider.settings import oauth2_settings
from oauth2_provider.views import AuthorizationView


class BBMSValidator(OAuth2Validator):
    def get_additional_claims(self):
        # Request-agnostic form: each claim is worked out from the request's user when the token is made.
        return {
            "email": lambda r: r.user.email,
            "email_verified": lambda r: r.user.email_verified,
            "given_name": lambda r: r.user.given_name,
            "family_name": lambda r: r.user.family_name,
            "name": lambda r: r.user.get_full_name(),
        }


class BBMSAuthorizationView(AuthorizationView):
    """The page Moodle sends members to. Off unless the signing key is set; banned members are refused."""

    def dispatch(self, request, *args, **kwargs):
        if not oauth2_settings.OIDC_ENABLED:
            raise Http404
        user = request.user
        if user.is_authenticated and getattr(user, "banned", False):
            return render(request, "moodle/banned.html", status=403)
        if user.is_authenticated and not user.email_verified:
            return render(request, "moodle/confirm_email.html")
        return super().dispatch(request, *args, **kwargs)
