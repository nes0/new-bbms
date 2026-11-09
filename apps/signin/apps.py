from django.apps import AppConfig


class SigninConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.signin"
    verbose_name = "Logging in"

    def ready(self):
        from . import checks  # noqa: F401  (registers the deploy checks)
