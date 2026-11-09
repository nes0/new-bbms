"""Deploy checks for logging in (spec 06 §6.11)."""

from django.conf import settings
from django.core import checks


@checks.register(checks.Tags.security, deploy=True)
def client_ip_header_is_set(app_configs, **kwargs):
    if settings.CLIENT_IP_HEADER:
        return []
    return [
        checks.Warning(
            "CLIENT_IP_HEADER isn't set, so logins and password resets are only limited per email address, not per "
            "visitor. On App Platform set it to HTTP_DO_CONNECTING_IP.",
            id="signin.W001",
        )
    ]
