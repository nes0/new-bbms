from django.apps import AppConfig


class BillingConfig(AppConfig):
    name = "apps.billing"
    verbose_name = "Subscriptions and billing"

    def ready(self):
        from . import checks  # noqa: F401  (registers the deploy check)
