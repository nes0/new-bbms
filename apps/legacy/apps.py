from django.apps import AppConfig


class LegacyConfig(AppConfig):
    """Importing data from the legacy (Laravel) system (spec 08 §8.2). No models of its own."""

    name = "apps.legacy"
    verbose_name = "Legacy import"
