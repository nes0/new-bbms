from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    """The ledger of payments, and member credit worked out from it (spec 04)."""

    name = "apps.payments"
    verbose_name = "Payments and credit"

    def ready(self):
        from . import checks  # noqa: F401  (registers the deploy check)
