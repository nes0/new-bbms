"""
Subscription and locker rent charges, and the GoCardless records behind them (spec 03 §3.3-3.6, 04 §4.2, 06 §6.2).

A **charge** is "this member owes this month's subscription (or this locker's rent), due on this date". It's the link
between billing and the date a member's membership is paid until. The money itself is in the ledger
(`payments.Payment`), one row per charge, pointing back at it.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Charge(models.Model):
    class Kind(models.TextChoices):
        SUBSCRIPTION = "subscription", "Subscription"
        LOCKER_RENT = "locker-rent", "Locker rent"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"  # created up to a week ahead
        DUE = "due", "Due"  # on or after its date, not yet billed
        PROCESSING = "processing", "Processing"  # a Direct Debit is on its way (or being retried)
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"  # the Direct Debit finally failed; the member can pay it another way
        CANCELLED = "cancelled", "Cancelled"

    OPEN = (Status.PENDING, Status.DUE, Status.PROCESSING)
    UNPAID = (Status.DUE, Status.FAILED)  # what a member can pay themselves

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="charges")
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.SUBSCRIPTION)
    rental = models.ForeignKey(
        "storage.LockerRental", on_delete=models.PROTECT, null=True, blank=True, related_name="charges"
    )
    charge_date = models.DateField()
    amount_pence = models.PositiveIntegerField(null=True, blank=True, help_text="Set when the charge is billed.")
    first_rent = models.BooleanField(
        default=False,
        help_text="The first rent for a locker rented by the member, for the days until their payment day. Its amount "
        "was set when they rented it.",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    paid_on = models.DateField(null=True, blank=True)
    # Failed Direct Debits (03 §3.6).
    failure_cause = models.CharField(
        max_length=100, blank=True, help_text="GoCardless's reason, e.g. insufficient_funds."
    )
    retries = models.PositiveSmallIntegerField(default=0, help_text="Times BBMS has asked GoCardless to try again.")
    retry_on = models.DateField(null=True, blank=True, help_text="When BBMS will next ask GoCardless to try again.")
    member_told_at = models.DateTimeField(
        null=True, blank=True, help_text="When the member was emailed that it couldn't be paid (once per charge)."
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    legacy_id = models.PositiveIntegerField(null=True, blank=True, unique=True)

    class Meta:
        ordering = ["-charge_date", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["member", "charge_date"],
                condition=Q(kind="subscription"),
                name="one_subscription_charge_per_member_per_date",
            ),
            models.UniqueConstraint(
                fields=["rental", "charge_date"],
                condition=Q(kind="locker-rent"),
                name="one_rent_charge_per_rental_per_date",
            ),
            models.CheckConstraint(
                condition=Q(kind="subscription", rental__isnull=True) | Q(kind="locker-rent", rental__isnull=False),
                name="charge_rental_only_for_rent",
            ),
        ]
        indexes = [models.Index(fields=["status", "charge_date"])]

    def __str__(self):
        what = self.get_kind_display() if self.rental_id is None else f"Rent for {self.rental.locker}"
        return f"{what} {self.charge_date:%d %b %Y} · {self.get_status_display()}"

    @property
    def reason(self):
        """The ledger reason for paying this charge."""
        from apps.payments.models import Payment

        return Payment.Reason.SUBSCRIPTION if self.kind == self.Kind.SUBSCRIPTION else Payment.Reason.LOCKER_RENT


class MandateSetup(models.Model):
    """A member setting up a Direct Debit through GoCardless's hosted pages (a billing request flow)."""

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mandate_setups")
    billing_request_id = models.CharField(max_length=50, unique=True)
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    mandate_id = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Direct Debit set-up for {self.member} ({self.started_at:%d %b %Y})"


class GoCardlessEvent(models.Model):
    """A webhook event from GoCardless, kept so duplicates are ignored and failures can be looked at (04 §4.2)."""

    event_id = models.CharField(max_length=50, unique=True)
    resource_type = models.CharField(max_length=50)
    action = models.CharField(max_length=50)
    resource_id = models.CharField(max_length=50, blank=True, help_text="The payment, mandate, etc.")
    created_at = models.DateTimeField(help_text="When GoCardless created the event.")
    received_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=255, blank=True, help_text="What BBMS did with it.")
    error = models.TextField(blank=True, help_text="Set when handling it failed; someone needs to look.")
    payload = models.JSONField(help_text="The event as GoCardless sent it (ids and codes, no names).")

    class Meta:
        ordering = ["-created_at", "-pk"]
        verbose_name = "GoCardless event"

    def __str__(self):
        return f"{self.resource_type}.{self.action} {self.resource_id}"
