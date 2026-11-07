"""
Equipment, inductions and usage sessions (spec 02 §2.6, 05 §5.3, §5.6–5.8).

All money is in pence.
"""

from django.conf import settings
from django.contrib.auth.models import Group
from django.db import models
from django.utils import timezone


class Equipment(models.Model):
    class Status(models.TextChoices):
        WORKING = "working", "Working"
        OUT_OF_ORDER = "out_of_order", "Out of order"

    class MeterMode(models.TextChoices):
        # 05 §5.4: some machines' running state can't be read reliably from their current draw.
        CURRENT = "current", "Current draw above a threshold"
        SESSION = "session", "Time a member is tapped in"

    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=64, unique=True, help_text="Also the device id of the machine's fob reader.")
    team = models.ForeignKey(
        Group,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="equipment",
        help_text="The role that looks after it: can reclassify and adjust sessions, and is notified of faults.",
    )
    manufacturer = models.CharField(max_length=100, blank=True)
    model_number = models.CharField(max_length=100, blank=True)
    serial_number = models.CharField(max_length=100, blank=True)
    asset_tag_id = models.CharField(max_length=50, blank=True)
    room = models.CharField(max_length=100, blank=True)
    description = models.TextField(blank=True)
    help_text = models.TextField(blank=True, help_text="Markdown.")
    ppe = models.CharField(max_length=255, blank=True, help_text="Protective equipment needed, comma separated.")

    requires_induction = models.BooleanField(default=False)
    induction_category = models.SlugField(
        max_length=64, blank=True, help_text="Machines sharing a category share one induction (e.g. two drills)."
    )
    induction_fee_pence = models.PositiveIntegerField(default=0)
    usage_cost_pence_per_hour = models.PositiveIntegerField(default=0)

    moodle_course_id = models.PositiveIntegerField(
        null=True, blank=True, help_text="Online course required (06 §6.12)."
    )
    moodle_course_since = models.DateTimeField(
        null=True, blank=True, help_text="When the course became required. Members trained before then keep access."
    )
    course_required_for_existing = models.BooleanField(
        default=False, help_text="Also require the course from members trained before it was added."
    )

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.WORKING)
    status_note = models.CharField(max_length=255, blank=True)
    status_since = models.DateTimeField(default=timezone.now)

    meter_mode = models.CharField(max_length=10, choices=MeterMode.choices, default=MeterMode.SESSION)
    current_threshold_amps = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    idle_auto_end_minutes = models.PositiveIntegerField(
        null=True, blank=True, help_text="Current-draw machines only. Empty = off."
    )
    service_interval_hours = models.PositiveIntegerField(null=True, blank=True, help_text="Empty = no reminders.")

    permaloan_member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="On long-term loan from this member.",
    )
    archived = models.BooleanField(default=False)
    obtained_at = models.DateField(null=True, blank=True)
    removed_at = models.DateField(null=True, blank=True)
    legacy_id = models.PositiveIntegerField(null=True, blank=True, unique=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "equipment"

    def __str__(self):
        return self.name

    @property
    def induction_key(self):
        """The category that inductions are recorded against."""
        return self.induction_category or self.slug


class Induction(models.Model):
    """A member's training on an induction category: paid for, then signed off by a trainer (05 §5.8)."""

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="inductions")
    category = models.SlugField(max_length=64, help_text="Equipment.induction_category (or slug).")
    paid = models.BooleanField(default=False)
    # TODO(payments app): link to the payment that paid the induction fee.
    trained_at = models.DateTimeField(null=True, blank=True)
    trainer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    is_trainer = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    legacy_id = models.PositiveIntegerField(null=True, blank=True, unique=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["member", "category"], name="one_induction_per_category")]

    def __str__(self):
        return f"{self.member} · {self.category}"

    @property
    def is_trained(self):
        return self.paid and self.trained_at is not None


class TrainingCompletion(models.Model):
    """A Moodle course a member has completed, as last checked (06 §6.12). Machines only ever use this record."""

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="course_completions")
    moodle_course_id = models.PositiveIntegerField()
    completed_at = models.DateTimeField()
    last_checked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["member", "moodle_course_id"], name="one_completion_per_course")]

    def __str__(self):
        return f"{self.member} · course {self.moodle_course_id}"


class UsageSession(models.Model):
    """One use of a machine, from tap-in to tap-out (05 §5.6)."""

    class Reason(models.TextChoices):
        NORMAL = "", "Normal use"
        TRAINING = "training", "Training"
        TESTING = "testing", "Testing"
        MAINTENANCE = "maintenance", "Maintenance"

    class EndedBy(models.TextChoices):
        TAP = "tap", "Tapped out"
        BUTTON = "button", "End button"
        DEVICE = "device", "Device (v1)"
        IDLE = "idle", "Machine idle"
        TIDY_UP = "tidy-up", "Hourly tidy-up"
        ADMIN = "admin", "Admin or team"

    equipment = models.ForeignKey(Equipment, on_delete=models.PROTECT, related_name="sessions")
    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="usage_sessions")
    fob = models.ForeignKey("access.KeyFob", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    started_at = models.DateTimeField(default=timezone.now)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.CharField(max_length=10, choices=EndedBy.choices, blank=True)
    reason = models.CharField(max_length=20, choices=Reason.choices, blank=True, default=Reason.NORMAL)
    removed = models.BooleanField(default=False, help_text="60 seconds or less: not charged, hidden.")
    motor_on_seconds = models.PositiveIntegerField(default=0)

    fee_pence = models.PositiveIntegerField(null=True, blank=True, help_text="Worked out when the session ends.")
    # TODO(payments app): charged_at is set when the fee is taken from the member's credit.
    charged_at = models.DateTimeField(null=True, blank=True)

    original_started_at = models.DateTimeField(null=True, blank=True)
    original_ended_at = models.DateTimeField(null=True, blank=True)
    adjusted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    adjusted_reason = models.CharField(max_length=255, blank=True)
    legacy_id = models.PositiveIntegerField(null=True, blank=True, unique=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["equipment", "ended_at"]), models.Index(fields=["member", "-started_at"])]

    def __str__(self):
        return f"{self.equipment} · {self.member} · {self.started_at:%Y-%m-%d %H:%M}"

    @property
    def is_open(self):
        return self.ended_at is None

    @property
    def duration_seconds(self):
        end = self.ended_at or timezone.now()
        return max(0, int((end - self.started_at).total_seconds()))
