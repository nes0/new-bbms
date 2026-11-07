"""
Values the trustees can change without a code change (spec 02 §2.8).

One row (`SiteSettings.load()`) with one typed field per setting, so each value is validated and has a label and
help text in the admin. Every change is recorded in `SettingChange` (who, when, old and new value).

To add a setting: add a field here with the current value as its default, add it to a group in GROUPS, make a
migration, and read it with `SiteSettings.load().<field>` where the constant used to be. Money fields end in
`_pence` (the admin shows them in pounds); add settings only when something uses them.

Settings are read from the database each time (one quick query), so a change applies at once, on every server.
"""

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


def whole_pounds(pence):
    if pence % 100:
        raise ValidationError("Enter whole pounds.")


class SiteSettings(models.Model):
    # --- the site ---
    space_name = models.CharField(
        "space name", max_length=100, default="Build Brighton", help_text="Shown on every page and in emails."
    )

    # --- membership (03) ---
    minimum_subscription_pence = models.PositiveIntegerField(
        "minimum monthly subscription",
        default=2500,
        validators=[MinValueValidator(100), whole_pounds],
        help_text="The least a member can choose to pay each month when joining or changing their amount "
        "(03 §3.1, §3.7). Members already paying less keep their amount until they change it.",
    )

    # --- fixed prices (04 §4.1) ---
    key_deposit_pence = models.PositiveIntegerField(
        "key deposit",
        default=1500,
        validators=[MinValueValidator(1)],
        help_text="For the physical key that gives 24-hour access (02 §2.2). New payments only.",
    )
    box_fee_pence = models.PositiveIntegerField(
        "box fee",
        default=1500,
        validators=[MinValueValidator(1)],
        help_text="One-off, non-refundable fee for a member's storage box (06 §6.2). New payments only.",
    )

    # --- machines (05 §5.3, §5.6) ---
    minimum_session_minutes = models.PositiveIntegerField(
        "minimum charge for a machine session (minutes)",
        default=15,
        validators=[MinValueValidator(0), MaxValueValidator(240)],
        help_text="A charged session costs at least this much time. Sessions of 60 seconds or less are always free.",
    )
    machine_credit_limit_pence = models.PositiveIntegerField(
        "credit below zero still allowed to start a machine session",
        default=500,
        validators=[MaxValueValidator(10_000)],
        help_text="A member can start a charged session while their credit is above minus this amount (04 §4.1). "
        "A session that has started is always charged in full, even if that takes credit lower.",
    )

    # --- fob readers (05 §5.4) ---
    reader_check_in_seconds = models.PositiveIntegerField(
        "how often v2 readers check in (seconds)",
        default=300,
        validators=[MinValueValidator(60), MaxValueValidator(3600)],
        help_text="Also how quickly readers notice a lost fob or a revoked member.",
    )
    session_heartbeat_seconds = models.PositiveIntegerField(
        "how often machine readers report a session in use (seconds)",
        default=60,
        validators=[MinValueValidator(15), MaxValueValidator(600)],
    )

    # --- key fobs (05 §5.1) ---
    max_active_fobs = models.PositiveIntegerField(
        "active fobs a member may hold",
        default=2,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text="Fobs are free. A lost fob doesn't count.",
    )
    fob_enrolment_seconds = models.PositiveIntegerField(
        "how long a reader waits for a new fob to be tapped (seconds)",
        default=60,
        validators=[MinValueValidator(15), MaxValueValidator(600)],
        help_text='The window opened by "Tap new fob on a reader" on a member\'s admin page.',
    )

    # --- privacy (03 §3.1) ---
    id_document_max_age_days = models.PositiveIntegerField(
        "delete unchecked proofs of identity after (days)",
        default=30,
        validators=[MinValueValidator(1), MaxValueValidator(90)],
        help_text="Uploads a trustee hasn't checked by then are deleted.",
    )

    # How the admin groups them.
    GROUPS = (
        ("The site", ("space_name",)),
        ("Membership", ("minimum_subscription_pence",)),
        ("Prices", ("key_deposit_pence", "box_fee_pence")),
        ("Machines", ("minimum_session_minutes", "machine_credit_limit_pence")),
        ("Key fobs", ("max_active_fobs", "fob_enrolment_seconds")),
        ("Fob readers", ("reader_check_in_seconds", "session_heartbeat_seconds")),
        ("Privacy", ("id_document_max_age_days",)),
    )

    class Meta:
        verbose_name = "settings"
        verbose_name_plural = "settings"

    def __str__(self):
        return "Settings"

    def save(self, *args, **kwargs):
        self.pk = 1  # there is only ever one row
        stored = SiteSettings.objects.filter(pk=1).values("minimum_session_minutes").first()
        super().save(*args, **kwargs)
        if stored and stored["minimum_session_minutes"] != self.minimum_session_minutes:
            # Machine readers show the minimum charge: tell them their machine's details changed (09 §9.4).
            from apps.equipment.models import Equipment

            Equipment.objects.update(config_version=models.F("config_version") + 1)

    @classmethod
    def load(cls) -> "SiteSettings":
        settings_row, _ = cls.objects.get_or_create(pk=1)
        return settings_row

    @property
    def minimum_subscription_pounds(self) -> int:
        return self.minimum_subscription_pence // 100

    @property
    def minimum_session(self) -> timedelta:
        return timedelta(minutes=self.minimum_session_minutes)

    @property
    def id_document_max_age(self) -> timedelta:
        return timedelta(days=self.id_document_max_age_days)

    def display(self, name) -> str:
        """A setting's value as people read it: money in pounds."""
        value = getattr(self, name)
        return f"£{value / 100:.2f}" if name.endswith("_pence") else str(value)


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
