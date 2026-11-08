"""Deploy check: Discord is how new members join (06 §6.14)."""

from django.core import checks

from . import client


@checks.register(checks.Tags.security, deploy=True)
def discord_is_configured(app_configs, **kwargs):
    if client.configured():
        return []
    missing = ", ".join(name for name in client.NEEDED if not getattr(client.settings, name))
    return [
        checks.Warning(
            f"Discord isn't configured ({missing}), so new members aren't asked to join the Discord server.",
            id="discord.W001",
        )
    ]
