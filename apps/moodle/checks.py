"""Deploy checks for Moodle (spec 06 §6.12). Moodle isn't needed to run the site, so these are warnings."""

from django.conf import settings
from django.core import checks

from apps.site_settings.models import SiteSettings


@checks.register(checks.Tags.security, deploy=True)
def moodle_is_set_up(app_configs, **kwargs):
    warnings = []
    if not settings.OIDC_RSA_PRIVATE_KEY:
        warnings.append(
            checks.Warning(
                'OIDC_RSA_PRIVATE_KEY isn\'t set, so "Log in with BBMS" for Moodle is switched off.',
                hint="Make a key with: openssl genrsa 3072 (see docs/deployment.md, Moodle).",
                id="moodle.W001",
            )
        )
    if not settings.MOODLE_TOKEN:
        warnings.append(
            checks.Warning(
                "MOODLE_TOKEN isn't set, so BBMS can't check whether members have finished their online courses.",
                id="moodle.W002",
            )
        )
    try:
        has_address = bool(SiteSettings.load().moodle_url)
    except Exception:  # noqa: BLE001 - the database may not be reachable while checks run (e.g. at build time)
        return warnings
    if not has_address:
        warnings.append(
            checks.Warning(
                "Moodle's address isn't set (Settings → Moodle), so course links and checks are switched off.",
                id="moodle.W003",
            )
        )
    return warnings
