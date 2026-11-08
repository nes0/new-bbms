from django.apps import AppConfig


class DiscordConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.discord"
    label = "discord_link"  # "discord" is a well-known package name; keep the app label distinct
    verbose_name = "Discord"

    def ready(self):
        from . import checks  # noqa: F401
