"""
Access control records (spec 02 §2.5, 05 §5.1–5.5).
"""

import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

from . import tags


class KeyFob(models.Model):
    """An RFID fob given to a member. Never deleted: lost fobs are marked inactive so history is kept."""

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="fobs")
    # The full 12-hex tag when known. Fobs enrolled from a decimal-only reader only have a card number.
    tag_id = models.CharField(max_length=12, unique=True, null=True, blank=True)
    card_number = models.PositiveBigIntegerField(unique=True, null=True, blank=True)
    active = models.BooleanField(default=True)
    issued_at = models.DateTimeField(default=timezone.now)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    lost_at = models.DateTimeField(null=True, blank=True)
    legacy_id = models.PositiveIntegerField(null=True, blank=True, unique=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(tag_id__isnull=False) | models.Q(card_number__isnull=False),
                name="keyfob_has_tag_or_card_number",
            )
        ]

    def __str__(self):
        return self.tag_id or tags.format_card_number(self.card_number)

    def save(self, *args, **kwargs):
        if self.tag_id:
            self.tag_id = tags.normalise(self.tag_id)
            if self.card_number is None:
                self.card_number = tags.card_number(self.tag_id)
        super().save(*args, **kwargs)

    def mark_lost(self):
        self.active = False
        self.lost_at = timezone.now()
        self.save(update_fields=["active", "lost_at"])


def _hash_api_key(raw_key: str) -> str:
    # Device keys are long random strings, so a plain SHA-256 is enough; we never store the key itself.
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


class Device(models.Model):
    """A fob reader: the front door, an exit reader, or a machine's reader."""

    class Role(models.TextChoices):
        ENTRY = "entry", "Entry (front door)"
        EXIT = "exit", "Exit reader"
        EQUIPMENT = "equipment", "Equipment"

    name = models.CharField(max_length=100)
    device_id = models.SlugField(
        max_length=64, unique=True, help_text="Sent by the device; matches the equipment slug."
    )
    role = models.CharField(max_length=20, choices=Role.choices)
    api_key_hash = models.CharField(max_length=64, unique=True, editable=False)
    monitor_heartbeat = models.BooleanField(default=False, help_text="Warn the acs team if it goes quiet.")
    last_boot = models.DateTimeField(null=True, blank=True)
    last_heartbeat = models.DateTimeField(null=True, blank=True)
    # Device API v2 (spec 09 §9.4).
    firmware_version = models.CharField(max_length=50, blank=True)
    reported_cache_version = models.CharField(max_length=20, blank=True, help_text="What the device says it holds.")
    enabled = models.BooleanField(default=True, help_text="Untick to make the device refuse everyone (v2 firmware).")
    pending_commands = models.JSONField(default=list, blank=True, help_text="Sent once at the next check-in.")
    legacy_id = models.PositiveIntegerField(null=True, blank=True, unique=True)

    COMMANDS = ("refresh_cache", "purge_cache", "reboot")

    def __str__(self):
        return f"{self.name} ({self.device_id})"

    def queue_command(self, command: str):
        if command not in self.COMMANDS:
            raise ValueError(f"Unknown device command: {command}")
        if command not in self.pending_commands:
            self.pending_commands = [*self.pending_commands, command]
            self.save(update_fields=["pending_commands"])

    def set_api_key(self, raw_key: str):
        self.api_key_hash = _hash_api_key(raw_key)

    def generate_api_key(self) -> str:
        """Give the device a new key. Returns it once; only its hash is kept."""
        raw_key = secrets.token_urlsafe(32)
        self.set_api_key(raw_key)
        return raw_key

    @classmethod
    def for_api_key(cls, raw_key: str):
        if not raw_key:
            return None
        return cls.objects.filter(api_key_hash=_hash_api_key(raw_key)).first()


class AccessEvent(models.Model):
    """Every fob tap at a door or exit reader, allowed or not (spec 05 §5.2, 06 §6.7)."""

    class Kind(models.TextChoices):
        ENTRY = "entry", "Entry"  # a keyholder opened the door
        CHECK_IN = "check-in", "Check-in"  # a member who isn't a keyholder tapped in; door stays shut
        EXIT = "exit", "Exit"  # tapped the exit reader
        REFUSED = "refused", "Refused"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="access_events"
    )
    fob = models.ForeignKey(KeyFob, on_delete=models.SET_NULL, null=True, blank=True, related_name="events")
    device = models.ForeignKey(Device, on_delete=models.SET_NULL, null=True, blank=True, related_name="events")
    reason = models.CharField(max_length=50, blank=True, help_text="Why it was refused or checked in.")
    occurred_at = models.DateTimeField(help_text="When it happened (as reported by the device if delayed).")
    received_at = models.DateTimeField(default=timezone.now)
    delayed = models.BooleanField(default=False, help_text="Reported by the device more than a minute late.")

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [models.Index(fields=["member", "-occurred_at"]), models.Index(fields=["-occurred_at"])]

    def __str__(self):
        return f"{self.get_kind_display()} · {self.member or 'unknown'} · {self.occurred_at:%Y-%m-%d %H:%M}"
