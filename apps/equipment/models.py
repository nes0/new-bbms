"""
Equipment, inductions and usage sessions (spec 02 §2.6, 05 §5.3, §5.6–5.8).

All money is in pence.
"""

import uuid
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import Group
from django.db import models, transaction
from django.db.models import F
from django.db.models.signals import post_delete
from django.dispatch import receiver
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
    induction_fee_pence = models.PositiveIntegerField(
        "access fee (pence)",
        default=0,
        help_text="One-off equipment access fee a member pays before being signed off; it goes towards the machine's "
        "upkeep and eventual replacement.",
    )
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

    # Bumped whenever something a reader shows or uses changes, so readers know to re-read it (spec 09 §9.4).
    config_version = models.PositiveIntegerField(default=1, editable=False)
    # Set when the "service due" reminder goes to the team; cleared when a service is recorded.
    service_reminder_sent_at = models.DateTimeField(null=True, blank=True, editable=False)

    permaloan_member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="On long-term loan from this member.",
    )
    archived = models.BooleanField(
        "retired",
        default=False,
        help_text="Disposed of or no longer in the space. Retired equipment is kept, with its history, for review.",
    )
    obtained_at = models.DateField(null=True, blank=True)
    removed_at = models.DateField("retired on", null=True, blank=True)
    purchase_price_pence = models.PositiveIntegerField(
        "bought for (pence)",
        null=True,
        blank=True,
        help_text="What the space paid for it, if known: shown against its fees when it's retired.",
    )
    legacy_id = models.PositiveIntegerField(null=True, blank=True, unique=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "equipment"

    CONFIG_FIELDS = (
        "name",
        "usage_cost_pence_per_hour",
        "status",
        "status_note",
        "meter_mode",
        "current_threshold_amps",
        "idle_auto_end_minutes",
    )

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # Bump config_version here, so the admin, the API and any future page all do it (spec 09 §9.4).
        bumped = False
        if self.pk and not self._state.adding:
            stored = Equipment.objects.filter(pk=self.pk).values(*self.CONFIG_FIELDS).first()
            changed = stored is not None and any(
                _same_config_value(stored[f], getattr(self, f)) is False for f in self.CONFIG_FIELDS
            )
            if changed:
                bumped = True
                self.config_version = F("config_version") + 1
                update_fields = kwargs.get("update_fields")
                if update_fields is not None:
                    kwargs["update_fields"] = {*update_fields, "config_version"}
        super().save(*args, **kwargs)
        if bumped:
            self.refresh_from_db(fields=["config_version"])

    @property
    def induction_key(self):
        """The category that inductions are recorded against."""
        return self.induction_category or self.slug


def _same_config_value(stored, new) -> bool:
    if stored is None or new is None:
        return stored is new
    if isinstance(stored, Decimal):
        return stored == Decimal(str(new))
    return stored == new


def _photo_path(photo, filename):
    return f"equipment-photos/{uuid.uuid4().hex}.jpg"


class EquipmentPhoto(models.Model):
    """A photo of a machine (05 §5.7): a 1000 px square and a 300 px thumbnail, as JPEGs, in private file storage,
    served to members through BBMS. Made by `apps.equipment.photos`; legacy's are copied by the import (08 §8.2)."""

    equipment = models.ForeignKey(Equipment, on_delete=models.CASCADE, related_name="photos")
    image = models.FileField(upload_to=_photo_path, editable=False)
    thumbnail = models.FileField(upload_to=_photo_path, editable=False)
    position = models.PositiveSmallIntegerField(default=0, help_text="Lower comes first; the first is the main photo.")
    uploaded_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["position", "pk"]
        verbose_name = "photo"

    def __str__(self):
        return f"{self.equipment} · photo {self.pk}"


@receiver(post_delete, sender=EquipmentPhoto)
def _delete_equipment_photo_files(sender, instance, **kwargs):
    """Deleting a photo (or its machine) deletes its files, once the deletion is committed."""
    names = [f.name for f in (instance.image, instance.thumbnail) if f]
    storage = instance.image.storage
    transaction.on_commit(lambda: [storage.delete(name) for name in names])


class Induction(models.Model):
    """A member's training on an induction category: paid for, then signed off by a trainer (05 §5.8)."""

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="inductions")
    category = models.SlugField(max_length=64, help_text="Equipment.induction_category (or slug).")
    paid = models.BooleanField(default=False)
    payment = models.ForeignKey(
        "payments.Payment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="The payment of the induction fee, if it was paid through BBMS.",
    )
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
    # An admin can clear a completion (they must retake the course). It then counts for nothing until Moodle reports
    # a completion later than this (06 §6.12).
    cleared_at = models.DateTimeField(null=True, blank=True)
    cleared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [models.UniqueConstraint(fields=["member", "moodle_course_id"], name="one_completion_per_course")]

    def __str__(self):
        return f"{self.member} · course {self.moodle_course_id}"

    @property
    def counts(self):
        """Whether it still counts: not cleared."""
        return self.cleared_at is None


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
    # When the fee was settled with the member's credit (payments.services.settle_session). The charge, and any
    # later corrections, are ledger rows linked to the session (`payments`).
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


class MachineStatusChange(models.Model):
    """History behind the out-of-order button and the admin's status field (spec 09 §9.4)."""

    class Source(models.TextChoices):
        BUTTON = "button", "Button on the machine"
        ADMIN = "admin", "Admin"
        WEB = "web", "Equipment page"

    equipment = models.ForeignKey(Equipment, on_delete=models.CASCADE, related_name="status_changes")
    status = models.CharField(max_length=20, choices=Equipment.Status.choices)
    note = models.CharField(max_length=255, blank=True)
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    session = models.ForeignKey(UsageSession, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    source = models.CharField(max_length=10, choices=Source.choices)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.equipment} · {self.status} · {self.created_at:%Y-%m-%d %H:%M}"


class MeterReading(models.Model):
    """Seconds the motor ran, as reported by a reader in current-draw mode (spec 09 §9.6)."""

    device = models.ForeignKey("access.Device", on_delete=models.CASCADE, related_name="meter_readings")
    equipment = models.ForeignKey(Equipment, on_delete=models.CASCADE, related_name="meter_readings")
    session = models.ForeignKey(UsageSession, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    seq = models.PositiveBigIntegerField()
    active_seconds = models.PositiveIntegerField()
    received_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["device", "seq"], name="one_reading_per_seq")]
        indexes = [models.Index(fields=["equipment", "received_at"])]

    def __str__(self):
        return f"{self.equipment} · {self.active_seconds}s · #{self.seq}"


class ServiceRecord(models.Model):
    equipment = models.ForeignKey(Equipment, on_delete=models.CASCADE, related_name="service_records")
    serviced_at = models.DateField(default=timezone.localdate)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-serviced_at", "-created_at"]

    def __str__(self):
        return f"{self.equipment} serviced {self.serviced_at}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # A recorded service re-arms the "service due" reminder.
        Equipment.objects.filter(pk=self.equipment_id).update(service_reminder_sent_at=None)


class OfflineEvent(models.Model):
    """An event a device stored while BBMS was unreachable and sent later (spec 09 §9.7)."""

    device = models.ForeignKey("access.Device", on_delete=models.CASCADE, related_name="offline_events")
    event_id = models.CharField(max_length=64)
    type = models.CharField(max_length=20)
    payload = models.JSONField(default=dict)
    received_at = models.DateTimeField(default=timezone.now)
    result = models.CharField(max_length=20)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["device", "event_id"], name="one_offline_event_per_id")]

    def __str__(self):
        return f"{self.device} · {self.event_id} · {self.result}"
