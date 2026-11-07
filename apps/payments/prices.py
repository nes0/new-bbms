"""Fixed prices and credit limits, in pence (spec 02 §2.8, 04 §4.1).

The key deposit and box fee are admin settings (apps/site_settings); read them through these functions so the
amount is always the current setting, never something from a form.
"""

from apps.site_settings.models import SiteSettings


def key_deposit_pence() -> int:
    return SiteSettings.load().key_deposit_pence


def box_fee_pence() -> int:
    return SiteSettings.load().box_fee_pence


# Paying from credit may not take it below this (04 §4.1). Machine use has its own rule (apps.equipment.rules).
MINIMUM_CREDIT_PENCE = 0
