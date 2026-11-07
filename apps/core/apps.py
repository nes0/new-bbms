from django.apps import AppConfig


class CoreConfig(AppConfig):
    """Site-wide pieces: the home page, the health check, shared templates."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
