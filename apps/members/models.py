"""
Members: the people who belong to the space, and the login accounts they use.

The spec is in docs/spec/02-data-model.md (§2.2) and 03-membership-lifecycle.md. This is the first
cut: identity, status and the flags that control access. Payment details, fobs, inductions and so on
arrive with their own apps.
"""

import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models, transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
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

    class PaymentMethod(models.TextChoices):
        # 02 §2.2. Old-style GoCardless subscriptions are dropped (03 §3.7).
        NONE = "", "None"
        DIRECT_DEBIT = "gocardless-variable", "Direct Debit"
        CREDIT = "balance", "Member credit"
        STANDING_ORDER = "standing-order", "Standing order"
        PAYPAL = "paypal", "PayPal"
        CASH = "cash", "Cash"

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
    # The general induction (05 §5.9): the member reads the member agreement and the rules and says they've done it
    # (`induction_completed`); someone with the "confirm general inductions" permission then confirms it.
    induction_completed = models.BooleanField(default=False, help_text="Completed the online general induction.")
    inducted_by = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="induction confirmed by",
    )
    inducted_at = models.DateTimeField("induction confirmed", null=True, blank=True)
    rules_agreed_at = models.DateTimeField(null=True, blank=True)
    # Whole pounds in the legacy system; pence here (02 §2.2). The minimums are settings (02 §2.8).
    monthly_subscription_pence = models.PositiveIntegerField(null=True, blank=True)
    # Billing (03 §3.3-3.7). A credit payer with a mandate has Direct Debit as their backup.
    payment_method = models.CharField(max_length=30, choices=PaymentMethod.choices, blank=True, default="")
    payment_day = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Day of the month the subscription is charged (1-28)."
    )
    mandate_id = models.CharField("GoCardless mandate", max_length=50, blank=True)
    discord_user_id = models.CharField(
        "Discord account",
        max_length=32,
        blank=True,
        help_text="Discord's id for the member's linked account (06 §6.14).",
    )
    discord_username = models.CharField(max_length=100, blank=True)
    discord_linked_at = models.DateTimeField(null=True, blank=True)
    discord_on_server = models.BooleanField(
        default=False, help_text="On the space's Discord server when BBMS last looked."
    )
    discord_welcome_due = models.BooleanField(
        default=False, help_text="BBMS added them to the server and hasn't posted their welcome message yet."
    )
    discord_exempt = models.BooleanField(
        "Discord not needed",
        default=False,
        help_text="Lets this member be set up without linking a Discord account (06 §6.14). For trustees to use "
        "sparingly.",
    )
    paypal_email = models.EmailField(
        "PayPal email",
        blank=True,
        help_text="The member's PayPal address, if it isn't their email address. PayPal payments are matched by it.",
    )
    paypal_subscription_id = models.CharField(
        "PayPal subscription",
        max_length=50,
        blank=True,
        db_index=True,
        help_text="PayPal's id for the member's current subscription (04 §4.4).",
    )
    subscription_expires = models.DateField(
        "paid until", null=True, blank=True, help_text="Membership is paid up to this date (03 §3.3)."
    )
    suspended_at = models.DateTimeField(null=True, blank=True, help_text="When the member was last suspended.")
    profile_private = models.BooleanField(default=False, help_text="Hide from the public member list.")
    # Needed for door access (05 §5.2). These become part of the profile/photo app when it's built.
    photo_approved = models.BooleanField(default=False, help_text="Has an approved photo on the system.")
    photo_on_wall = models.BooleanField(default=False, help_text="Has a physical photo on the wall instead.")

    email_verified_at = models.DateTimeField(
        "email confirmed at",
        null=True,
        blank=True,
        help_text="When the member showed they can read mail sent to this address, by following the link we emailed "
        "(03 §3.1). Empty: not confirmed. Changing the address clears it.",
    )

    box_paid_at = models.DateTimeField(
        "box paid on",
        null=True,
        blank=True,
        help_text="When the member paid the one-off box fee (06 §6.2). Empty: no box flag. Change it with the rules in "
        "apps/storage/rules.py, which log it.",
    )
    box_payment = models.ForeignKey(
        "payments.Payment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="The payment that set the box flag; empty if the storage team ticked it for a fee paid outside BBMS.",
    )

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
        permissions = [("confirm_general_inductions", "Can confirm members' general inductions")]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(payment_day__isnull=True) | models.Q(payment_day__gte=1, payment_day__lte=28),
                name="member_payment_day_1_to_28",
            ),
            # One BBMS account per Discord account.
            models.UniqueConstraint(
                fields=["discord_user_id"],
                condition=~models.Q(discord_user_id=""),
                name="one_member_per_discord_account",
            ),
        ]

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
    def box_paid(self) -> bool:
        """Whether the member has paid for a box (06 §6.2). There is no box inventory: only this flag."""
        return self.box_paid_at is not None

    @property
    def email_verified(self) -> bool:
        return self.email_verified_at is not None

    @property
    def has_space_access(self):
        """Whether this member's status lets them be in the space at all (door and machines add more rules)."""
        return self.status in self.STATUSES_WITH_ACCESS and not self.banned

    @property
    def general_induction_status(self):
        """ "confirmed", "waiting" (done, not yet confirmed) or "to-do" (05 §5.9)."""
        # Confirmed stays confirmed if whoever confirmed it is later deleted (inducted_by is then empty).
        if self.induction_completed and (self.inducted_by_id or self.inducted_at):
            return "confirmed"
        return "waiting" if self.induction_completed else "to-do"

    @property
    def induction_confirmed(self):
        """Needed before a member can be a key holder and open the door (05 §5.2, §5.9)."""
        return self.general_induction_status == "confirmed"

    @property
    def has_direct_debit(self):
        return bool(self.mandate_id)

    @property
    def is_billable(self):
        """Gets a monthly subscription charge (03 §3.6): active and not honorary."""
        return self.status == self.Status.ACTIVE

    @property
    def current_address(self):
        """The address a trustee has approved, if any."""
        return self.addresses.filter(approved=True).first()

    @property
    def pending_address(self):
        """A change the member asked for that hasn't been approved yet (newer than the current address)."""
        pending = self.addresses.filter(approved=False).first()
        current = self.current_address
        if pending is not None and (current is None or pending.created_at > current.created_at):
            return pending
        return None


class MemberChange(models.Model):
    """The audit log for members (02 §2.3, 03 §3.9): who changed a member's trusted, key holder, induction, photo or
    banned flags, and when. Written by apps/members/changes.py; rows imported from the legacy audit log have the
    field "legacy" and the legacy description as the new value.
    """

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="changes")
    field = models.CharField(max_length=50)
    old_value = models.CharField(max_length=255, blank=True)
    new_value = models.CharField(max_length=255, blank=True)
    changed_by = models.ForeignKey(Member, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    changed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-changed_at", "-pk"]
        verbose_name = "change to a member (audit log)"
        verbose_name_plural = "changes to members (audit log)"

    def __str__(self):
        return f"{self.member} · {self.field}: {self.old_value} → {self.new_value}"


def _photo_path(photo, filename):
    # A random name, so a photo's address says nothing about whose it is (legacy used md5 of the member's id, 02 §2.2).
    return f"member-photos/{uuid.uuid4().hex}.png"


class MemberPhoto(models.Model):
    """A member's photo (06 §6.1): a 500 px square and a 200 px thumbnail, as PNGs, in private file storage.

    A member has at most one current (approved) photo and one waiting for approval. `Member.photo_approved` says
    whether they have an approved photo (05 §5.2). The upload and approval pages come with the profiles app; for now
    photos arrive from the legacy import.
    """

    class Kind(models.TextChoices):
        CURRENT = "current", "Approved"
        PENDING = "pending", "Waiting for approval"

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="photos")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    image = models.FileField(upload_to=_photo_path)
    thumbnail = models.FileField(upload_to=_photo_path)
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["member", "kind"], name="one_member_photo_per_kind")]

    def __str__(self):
        return f"{self.member} · {self.get_kind_display()}"


@receiver(post_delete, sender=MemberPhoto)
def _delete_photo_files(sender, instance, **kwargs):
    """Photos are personal data: deleting one (or its member) deletes its files, once the deletion is committed."""
    names = [f.name for f in (instance.image, instance.thumbnail) if f]
    storage = instance.image.storage
    transaction.on_commit(lambda: [storage.delete(name) for name in names])


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

    @property
    def lines(self):
        return [line for line in (self.line_1, self.line_2, self.line_3, self.line_4, self.postcode) if line]

    def approve(self):
        """Make this the member's address: earlier addresses (approved or not) are deleted, as they're out of date."""
        with transaction.atomic():
            Address.objects.filter(member=self.member_id).exclude(pk=self.pk).delete()
            self.approved = True
            self.save(update_fields=["approved"])


class IdDocument(models.Model):
    """A scan or photo a new member uploads as proof of identity (spec 03 §3.1, "Proof of identity").

    Kept only until a trustee has checked it, then deleted ("shredded"); unchecked uploads are deleted after the
    time in Settings (30 days to start with). The file is held in the database rather than in file storage, so there
    is exactly one copy and deleting the row deletes the file. Never registered in the Django admin, and never copied
    to the shadow site.
    """

    class Kind(models.TextChoices):
        PHOTO_ID = "photo_id", "Photo ID (e.g. passport or driving licence)"
        PROOF_OF_ADDRESS = "proof_of_address", "Proof of address (e.g. a recent utility bill)"

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="id_documents")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=50)
    size = models.PositiveIntegerField()
    # Encrypted (Fernet, with the keys in settings.ID_DOCUMENT_KEYS), so database backups and dumps only ever hold
    # ciphertext. Read and write it through id_documents.save_document() and id_documents.decrypt(). See 03 §3.1.
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
