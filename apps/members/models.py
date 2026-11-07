"""
Members: the people who belong to the space, and the login accounts they use.

The spec is in docs/spec/02-data-model.md (§2.2) and 03-membership-lifecycle.md. This is the first
cut: identity, status and the flags that control access. Payment details, fobs, inductions and so on
arrive with their own apps.
"""

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


class MemberManager(BaseUserManager):
    use_in_migrations = True

    def _create(self, email, password, **fields):
        if not email:
            raise ValueError("Members must have an email address.")
        member = self.model(email=self.normalize_email(email).lower(), **fields)
        member.set_password(password)
        member.save(using=self._db)
        return member

    def create_user(self, email, password=None, **fields):
        fields.setdefault("is_staff", False)
        fields.setdefault("is_superuser", False)
        return self._create(email, password, **fields)

    def create_superuser(self, email, password=None, **fields):
        fields.setdefault("is_staff", True)
        fields.setdefault("is_superuser", True)
        return self._create(email, password, **fields)


class Member(AbstractBaseUser, PermissionsMixin):
    class Status(models.TextChoices):
        # 03 §3.2. The legacy "payment-warning" and "on-hold" statuses are still to be confirmed as unused.
        SETTING_UP = "setting-up", "Setting up"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        LEAVING = "leaving", "Leaving"
        LEFT = "left", "Left"
        HONORARY = "honorary", "Honorary"

    # Statuses that may be in the space (05 §5.2; the exact list is a trustee decision, spec 01 §1.7).
    STATUSES_WITH_ACCESS = (Status.ACTIVE, Status.LEAVING, Status.HONORARY)

    email = models.EmailField("email address", unique=True)
    given_name = models.CharField(max_length=100)
    family_name = models.CharField(max_length=100)
    phone = models.CharField(max_length=30, blank=True)
    emergency_contact = models.CharField(max_length=255, blank=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SETTING_UP)
    trusted = models.BooleanField(default=False, help_text="Granted by an admin; needed before key holder.")
    key_holder = models.BooleanField(default=False, help_text="Has 24-hour access (physical key, deposit paid).")
    founder = models.BooleanField(default=False)
    induction_completed = models.BooleanField(default=False, help_text="Completed the online general induction.")
    rules_agreed_at = models.DateTimeField(null=True, blank=True)
    profile_private = models.BooleanField(default=False, help_text="Hide from the public member list.")
    # Needed for door access (05 §5.2). These become part of the profile/photo app when it's built.
    photo_approved = models.BooleanField(default=False, help_text="Has an approved photo on the system.")
    photo_on_wall = models.BooleanField(default=False, help_text="Has a physical photo on the wall instead.")

    banned = models.BooleanField(default=False)
    banned_at = models.DateTimeField(null=True, blank=True)
    banned_reason = models.TextField(blank=True)

    notes = models.TextField(blank=True, help_text="Admin notes; never shown to the member.")

    # Django's own flags. `is_active` means "this login works", which is NOT the same as an active membership.
    is_active = models.BooleanField(default=True, help_text="Untick to block this login entirely.")
    is_staff = models.BooleanField(default=False, help_text="Can use the admin site.")

    # The member's id in the legacy system, kept so migrated records can be traced (spec 08 §8.2).
    legacy_id = models.PositiveIntegerField(null=True, blank=True, unique=True)

    date_joined = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    objects = MemberManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = ["given_name", "family_name"]

    class Meta:
        ordering = ["given_name", "family_name"]

    def __str__(self):
        return self.full_name or self.email

    def save(self, *args, **kwargs):
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        return f"{self.given_name} {self.family_name}".strip()

    def get_full_name(self):
        return self.full_name

    def get_short_name(self):
        return self.given_name

    @property
    def has_space_access(self):
        """Whether this member's status lets them be in the space at all (door and machines add more rules)."""
        return self.status in self.STATUSES_WITH_ACCESS and not self.banned
