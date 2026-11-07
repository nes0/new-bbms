from django.apps import AppConfig


class AccessConfig(AppConfig):
    """Key fobs, access-control devices, door and exit decisions, and the device API (spec 05)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.access"
    verbose_name = "Access control"
