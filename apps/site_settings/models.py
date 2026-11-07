"""
Values the trustees can change without a code change (spec 02 §2.8).

One row (`SiteSettings.load()`) with one typed field per setting, so each value is validated and has a label and
help text in the admin. Every change is recorded in `SettingChange` (who, when, old and new value).

To add a setting: add a field here with the legacy value as its default, make a migration, and read it with
`SiteSettings.load().<field>` where the constant used to be.
"""

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


class SiteSettings(models.Model):
    minimum_subscription_pence = models.PositiveIntegerField(
        "minimum monthly subscription",
        default=2500,
        validators=[MinValueValidator(100)],
        help_text="The least a member can choose to pay each month when joining or changing their amount "
        "(03 §3.1, §3.7). Members already paying less keep their amount until they change it.",
    )

    class Meta:
        verbose_name = "settings"
        verbose_name_plural = "settings"

    def __str__(self):
        return "Settings"

    def save(self, *args, **kwargs):
        self.pk = 1  # there is only ever one row
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "SiteSettings":
        settings_row, _ = cls.objects.get_or_create(pk=1)
        return settings_row

    @property
    def minimum_subscription_pounds(self) -> int:
        return self.minimum_subscription_pence // 100


class SettingChange(models.Model):
    """The audit log for settings (02 §2.8: changes are audit-logged)."""

    setting = models.CharField(max_length=100)
    old_value = models.CharField(max_length=255)
    new_value = models.CharField(max_length=255)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    changed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-changed_at"]

    def __str__(self):
        return f"{self.setting}: {self.old_value} → {self.new_value}"
