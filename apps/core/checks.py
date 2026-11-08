"""Deploy checks for email (spec 06 §6.6)."""

from django.conf import settings
from django.core import checks

# Backends that don't deliver anything.
NOT_DELIVERED = {
    "django.core.mail.backends.console.EmailBackend",
    "django.core.mail.backends.dummy.EmailBackend",
    "django.core.mail.backends.locmem.EmailBackend",
    "django.core.mail.backends.filebased.EmailBackend",
}


@checks.register(checks.Tags.security, deploy=True)
def email_is_set_up(app_configs, **kwargs):
    if settings.EMAIL_BACKEND in NOT_DELIVERED:
        return [
            checks.Warning(
                "Email isn't set up, so BBMS only prints emails to its log: nobody gets welcome, payment or "
                "password reset emails.",
                hint="Set EMAIL_HOST, EMAIL_HOST_USER and EMAIL_HOST_PASSWORD (see docs/deployment.md).",
                id="core.W001",
            )
        ]
    warnings = []
    if settings.EMAIL_BACKEND.endswith("smtp.EmailBackend"):
        if not settings.EMAIL_HOST:
            warnings.append(checks.Warning("EMAIL_HOST isn't set, so email can't be sent.", id="core.W002"))
        elif not settings.EMAIL_USE_TLS and not settings.EMAIL_USE_SSL:
            warnings.append(
                checks.Warning(
                    "Email is sent without encryption, so its password and the messages can be read on the way.",
                    hint="Set EMAIL_USE_TLS=True (port 587) or EMAIL_USE_SSL=True (port 465).",
                    id="core.W003",
                )
            )
    return warnings
