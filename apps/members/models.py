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
    # Whole pounds in the legacy system; pence here (02 §2.2). The minimums are settings (02 §2.8).
    monthly_subscription_pence = models.PositiveIntegerField(null=True, blank=True)
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

    @property
    def display_name(self):
        """What readers may show: given name and the first letter of the family name ("Kim K.").

        One place on purpose: what devices may display is an open trustee question (spec 09 §9.3).
        """
        initial = f" {self.family_name[0]}." if self.family_name else ""
        return f"{self.given_name}{initial}".strip()

    def get_full_name(self):
        return self.full_name

    def get_short_name(self):
        return self.given_name

    @property
    def has_space_access(self):
        """Whether this member's status lets them be in the space at all (door and machines add more rules)."""
        return self.status in self.STATUSES_WITH_ACCESS and not self.banned


class Address(models.Model):
    """A member's postal address (02 §2.3). A member's own change waits for an admin to approve it."""

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="addresses")
    line_1 = models.CharField(max_length=255)
    line_2 = models.CharField(max_length=255, blank=True)
    line_3 = models.CharField(max_length=255, blank=True)
    line_4 = models.CharField(max_length=255, blank=True)
    postcode = models.CharField(max_length=10)
    approved = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "addresses"

    def __str__(self):
        return f"{self.line_1}, {self.postcode}"


class IdDocument(models.Model):
    """A scan or photo a new member uploads as proof of identity (spec 03 §3.1, "Proof of identity").

    Kept only until a trustee has checked it, then deleted ("shredded"); unchecked uploads are deleted after
    ID_DOCUMENT_MAX_AGE. The file is held in the database rather than in file storage, so there is exactly one copy
    and deleting the row deletes the file. Never registered in the Django admin, and never copied to the shadow site.
    """

    class Kind(models.TextChoices):
        PHOTO_ID = "photo_id", "Photo ID (e.g. passport or driving licence)"
        PROOF_OF_ADDRESS = "proof_of_address", "Proof of address (e.g. a recent utility bill)"

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="id_documents")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=50)
    size = models.PositiveIntegerField()
    # TODO(encryption): encrypt with a key from the environment (e.g. Fernet, `cryptography` package) so database
    # backups and dumps only ever hold ciphertext. See 03 §3.1.
    data = models.BinaryField()
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["member", "kind"], name="one_id_document_per_kind")]
        permissions = [("review_id_documents", "Can view and check new members' proofs of identity")]

    def __str__(self):
        return f"{self.member} · {self.get_kind_display()}"


class IdCheck(models.Model):
    """A trustee's check of a member's proofs of identity. Kept permanently; the documents themselves are not."""

    class Result(models.TextChoices):
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Not accepted: asked to send again"

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="id_checks")
    result = models.CharField(max_length=10, choices=Result.choices)
    note = models.CharField(
        max_length=255, blank=True, help_text='What was seen (e.g. "passport, council tax bill"), or why not.'
    )
    documents_seen = models.CharField(max_length=100, help_text="The kinds of document that were uploaded.")
    checked_by = models.ForeignKey(Member, on_delete=models.SET_NULL, null=True, related_name="+")
    checked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-checked_at"]
        verbose_name = "ID check"

    def __str__(self):
        return f"{self.member} · {self.get_result_display()} · {self.checked_at:%Y-%m-%d}"
