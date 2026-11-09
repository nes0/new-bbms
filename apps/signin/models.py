"""
Logging in safely (spec 06 §6.11): attempts counted for rate limiting, and two-factor login.

Attempts are kept in the database rather than the cache: the site runs several gunicorn workers, each with its own
memory, so a cache-based count would let an attacker spread guesses across them.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone


class Attempt(models.Model):
    """One counted attempt: a failed login, a password reset asked for, or a wrong two-factor code.

    `key` is a hash of what's being limited (an email address, an IP address or a member), never the value itself.
    """

    kind = models.CharField(max_length=30)
    key = models.CharField(max_length=64)
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=["kind", "key", "at"]), models.Index(fields=["at"])]

    def __str__(self):
        return f"{self.kind} at {self.at:%Y-%m-%d %H:%M}"


class TwoFactor(models.Model):
    """A member's authenticator app (TOTP, RFC 6238). Only counts once confirmed with a first code."""

    member = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="two_factor")
    secret = models.CharField(max_length=64, help_text="Base32. Shared with the member's authenticator app.")
    created_at = models.DateTimeField(default=timezone.now)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    # The last 30-second step a code was accepted for: the same code can't be used twice.
    last_step = models.BigIntegerField(default=0)

    class Meta:
        verbose_name = "two-factor login"
        verbose_name_plural = "two-factor logins"

    def __str__(self):
        return f"{self.member} ({'on' if self.confirmed_at else 'being set up'})"

    @property
    def is_on(self):
        return self.confirmed_at is not None


class RecoveryCode(models.Model):
    """A one-off code for logging in without the authenticator app. Only a hash is kept."""

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="recovery_codes")
    code_hash = models.CharField(max_length=128)
    used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.member} recovery code{' (used)' if self.used_at else ''}"
