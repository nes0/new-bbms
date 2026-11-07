from django.apps import AppConfig


class EquipmentConfig(AppConfig):
    """Machines and tools, inductions (training) and usage sessions (spec 05 §5.3, §5.6–5.8)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.equipment"
