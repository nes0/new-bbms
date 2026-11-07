from django.apps import AppConfig


class MembersConfig(AppConfig):
    """Members and their login accounts."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.members"

    def ready(self):
        from . import checks  # noqa: F401 (registers the startup checks)
