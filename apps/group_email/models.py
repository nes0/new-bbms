"""
Group email (spec 06 §6.6): who may email which group of members, and a record of what was sent.

Who may send to whom is decided by the admins, as rules (admin → Group email → Who can send group emails). Admins
themselves may email any group. The rules and the groups are applied in `audiences.py`.
"""

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class SenderRule(models.Model):
    """One rule: these senders may email this group of members."""

    class Sender(models.TextChoices):
        ROLE = "role", "Members of a role"
        MACHINE_TEAM = "machine-team", "Each machine's team (the role that looks after it)"
        MACHINE_TRAINERS = "machine-trainers", "Each machine's trainers"

    class Audience(models.TextChoices):
        MEMBERS = "members", "All current members"
        TRUSTED = "trusted", "Trusted members"
        KEY_HOLDERS = "key-holders", "Key holders"
        ROLE = "role", "Members of a role"
        TRAINED = "trained", "Members trained on a machine"
        WAITING = "waiting", "Members waiting for training on a machine"

    MACHINE_AUDIENCES = (Audience.TRAINED, Audience.WAITING)
    MACHINE_SENDERS = (Sender.MACHINE_TEAM, Sender.MACHINE_TRAINERS)

    sender = models.CharField("who can send", max_length=20, choices=Sender.choices, default=Sender.ROLE)
    sender_role = models.ForeignKey(
        Group,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="sending role",
        help_text="For “members of a role”.",
    )
    audience = models.CharField("to whom", max_length=20, choices=Audience.choices)
    audience_role = models.ForeignKey(
        Group,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="receiving role",
        help_text="For “members of a role”.",
    )
    category = models.SlugField(
        "machine",
        max_length=64,
        blank=True,
        help_text="For the machine groups, sent by a role: the machine (its induction category). Leave it empty for "
        "every machine the sending role looks after. A machine's team or trainers always email about their own "
        "machines, so leave it empty for them.",
    )

    class Meta:
        verbose_name = "who can send group emails"
        verbose_name_plural = "who can send group emails"

    def __str__(self):
        sender = f"The {self.sender_role} role" if self.sender == self.Sender.ROLE else self.get_sender_display()
        if self.audience == self.Audience.ROLE:
            audience = f"members of the {self.audience_role} role"
        elif self.audience in self.MACHINE_AUDIENCES:
            on = self.category or "their machines"
            audience = (
                f"members trained on {on}" if self.audience == self.Audience.TRAINED else f"members waiting for {on}"
            )
        else:
            audience = self.get_audience_display().lower()
        return f"{sender} → {audience}"

    def clean(self):
        errors = {}
        if self.sender == self.Sender.ROLE and self.sender_role_id is None:
            errors["sender_role"] = "Choose the role whose members can send."
        if self.sender != self.Sender.ROLE and self.sender_role_id is not None:
            errors["sender_role"] = "Only for “members of a role”."
        if self.sender in self.MACHINE_SENDERS and self.audience not in self.MACHINE_AUDIENCES:
            errors["audience"] = "A machine's team or trainers can only email about their machines."
        if self.sender in self.MACHINE_SENDERS and self.category:
            errors["category"] = "Leave it empty: a machine's team or trainers email about their own machines."
        if self.audience == self.Audience.ROLE and self.audience_role_id is None:
            errors["audience_role"] = "Choose the role whose members get the email."
        if self.audience != self.Audience.ROLE and self.audience_role_id is not None:
            errors["audience_role"] = "Only for “members of a role”."
        if self.audience not in self.MACHINE_AUDIENCES and self.category:
            errors["category"] = "Only for the machine groups."
        if errors:
            raise ValidationError(errors)


class GroupEmail(models.Model):
    """A group email that was sent: kept so the admins can see who emailed whom, and what."""

    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="group_emails")
    audience = models.CharField(max_length=100, help_text="Which group (its key, e.g. trained:laser).")
    audience_label = models.CharField("to", max_length=200)
    subject = models.CharField(max_length=150)
    body = models.TextField()
    sent_at = models.DateTimeField(default=timezone.now)
    recipients = models.PositiveIntegerField(default=0, help_text="How many members it was sent to.")
    failed = models.PositiveIntegerField(default=0, help_text="How many of them the mail server didn't take.")

    class Meta:
        ordering = ["-sent_at"]

    def __str__(self):
        return f"{self.subject} ({self.sent_at:%d %b %Y})"
