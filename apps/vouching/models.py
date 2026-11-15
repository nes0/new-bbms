"""
Vouching for members waiting for trusted status (spec 05 §5.10).

Active members vouch for (+1) or discreetly raise a concern about (−1) members who aren't trusted yet. Vouching is
advice to the trustees, not a vote: reaching the threshold only notifies them.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone


class Vouch(models.Model):
    """One voucher's current view of one waiting member. Withdrawing deletes it; `VouchChange` keeps the history."""

    class Value(models.IntegerChoices):
        VOUCH = 1, "Vouch"
        CONCERN = -1, "Concern"

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="vouches_received")
    voucher = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="vouches_given")
    value = models.SmallIntegerField(choices=Value.choices)
    note = models.TextField(blank=True, help_text="With a concern only; seen by the trustees, never the member.")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name_plural = "vouches"
        constraints = [
            models.UniqueConstraint(fields=["member", "voucher"], name="one_vouch_per_voucher"),
            models.CheckConstraint(condition=~models.Q(member=models.F("voucher")), name="no_vouching_for_yourself"),
        ]

    def __str__(self):
        return f"{self.voucher} → {self.member}: {self.get_value_display()}"


class VouchChange(models.Model):
    """Every vouch, concern and withdrawal, for the trustees (05 §5.10). Never edited."""

    class Action(models.TextChoices):
        VOUCH = "vouch", "Vouched"
        CONCERN = "concern", "Raised a concern"
        WITHDRAW = "withdraw", "Withdrew"

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    voucher = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    action = models.CharField(max_length=10, choices=Action.choices)
    note = models.TextField(blank=True)
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-at", "-pk"]
        indexes = [models.Index(fields=["member", "-at"])]

    def __str__(self):
        return f"{self.voucher} {self.get_action_display().lower()} for {self.member}"


class ThresholdNotice(models.Model):
    """The trustees were told this member's score reached the threshold. Once per member, ever."""

    member = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    score = models.SmallIntegerField()
    at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.member}: score {self.score}"
