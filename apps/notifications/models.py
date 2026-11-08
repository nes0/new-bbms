from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Notification(models.Model):
    """A short message in a member's own list at /notifications/ (spec 06 §6.5, 02 §2.3).

    `key` stops the same thing being said twice: a member has at most one notification with a given key (for
    example `trusted_status`, or `device-offline:<id>:<heartbeat>`). Notifications with no key are always added.
    """

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=100, blank=True, help_text="What it's about, e.g. trusted_status.")
    message = models.TextField()
    url = models.CharField(max_length=200, blank=True, help_text="A page on this site the message is about.")
    key = models.CharField(max_length=150, blank=True, help_text="Stops the same thing being said twice.")
    unread = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    legacy_id = models.PositiveIntegerField(null=True, blank=True, unique=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["member", "unread"])]
        constraints = [
            models.UniqueConstraint(
                fields=["member", "key"], condition=~Q(key=""), name="one_notification_per_member_and_key"
            )
        ]

    def __str__(self):
        return self.message[:60]
