"""Deploy checks for billing."""

from django.conf import settings
from django.core import checks


@checks.register(checks.Tags.security, deploy=True)
def gocardless_is_configured(app_configs, **kwargs):
    warnings = []
    if not settings.GOCARDLESS_ACCESS_TOKEN:
        warnings.append(
            checks.Warning(
                "GOCARDLESS_ACCESS_TOKEN isn't set, so nobody can set up or be billed by Direct Debit.",
                id="billing.W001",
            )
        )
    if not settings.GOCARDLESS_WEBHOOK_SECRET:
        warnings.append(
            checks.Warning(
                "GOCARDLESS_WEBHOOK_SECRET isn't set, so GoCardless's webhooks are refused and payments never "
                "show as paid.",
                id="billing.W002",
            )
        )
    if settings.GOCARDLESS_ENVIRONMENT not in ("sandbox", "live"):
        warnings.append(checks.Warning("GOCARDLESS_ENVIRONMENT must be 'sandbox' or 'live'.", id="billing.W003"))
    return warnings
