from django.conf import settings
from django.db import models
from django.utils import timezone


class MoodleAccount(models.Model):
    """Which Moodle user is this member (spec 06 §6.12, 02 §2.6).

    Found through Moodle's web services the first time BBMS needs to check a course, by the ID number Moodle holds
    for them (their BBMS id, sent as `sub`), falling back to their email address. Kept so later checks are one call.
    """

    member = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="moodle_account")
    moodle_user_id = models.PositiveIntegerField()
    found_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.member} · Moodle user {self.moodle_user_id}"
