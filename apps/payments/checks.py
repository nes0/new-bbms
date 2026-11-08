"""Deploy checks for card payments."""

from django.conf import settings
from django.core import checks


@checks.register(checks.Tags.security, deploy=True)
def stripe_is_configured(app_configs, **kwargs):
    warnings = []
    if not settings.STRIPE_SECRET_KEY:
        warnings.append(
            checks.Warning("STRIPE_SECRET_KEY isn't set, so members can't pay by card.", id="payments.W001")
        )
    if not settings.STRIPE_WEBHOOK_SECRET:
        warnings.append(
            checks.Warning(
                "STRIPE_WEBHOOK_SECRET isn't set, so Stripe's webhooks are refused (card payments are only recorded "
                "when the member comes back to BBMS).",
                id="payments.W002",
            )
        )
    return warnings
