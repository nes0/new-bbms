"""Feedback from members about the site (spec 06 §6.9)."""

from django.conf import settings
from django.db import models
from django.utils import timezone


class Feedback(models.Model):
    """A member's comments, emailed to the trustees. Kept too, so nothing is lost if the email can't be sent."""

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="feedback")
    comments = models.TextField()
    sent_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-sent_at"]
        verbose_name_plural = "feedback"

    def __str__(self):
        return f"Feedback from {self.member} ({self.sent_at:%d %b %Y})"
