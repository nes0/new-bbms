"""
Lockers: the inventory, who rents what and at what price (spec 06 §6.2). Money is in pence.

Prices, from most to least specific:
- a **rental's rent override**: what one member pays for one locker, set by a trustee with a reason. It belongs to
  that rental, so it ends when the rental ends;
- a locker's **advertised rent override**: the price shown for an empty locker and paid by whoever rents it next
  (it becomes their rental's override, and is cleared from the locker);
- the **default rent** for lockers of that size in that location (`LockerRent`).

A locker's status is worked out, not stored: **rented** if it has an open rental, otherwise **reserved**, **out of
use** or **available** (its `hold`). Rules that change any of this are in rules.py, which also logs each change.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

SORT_ORDER_HELP = "Lower numbers are listed first; ties are listed alphabetically."


class StorageLocation(models.Model):
    """Where lockers are in the space, e.g. "Workshop wall"."""

    name = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=255, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=0, help_text=SORT_ORDER_HELP)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class LockerSize(models.Model):
    """A physical size of locker, e.g. "Large (60 × 45 × 45 cm)"."""

    name = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=255, blank=True, help_text="Inside dimensions, what fits, etc.")
    sort_order = models.PositiveSmallIntegerField(default=0, help_text=SORT_ORDER_HELP)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class LockerRent(models.Model):
    """The default monthly rent for lockers of one size in one location."""

    location = models.ForeignKey(StorageLocation, on_delete=models.CASCADE, related_name="rents")
    size = models.ForeignKey(LockerSize, on_delete=models.CASCADE, related_name="rents")
    monthly_rent_pence = models.PositiveIntegerField("default monthly rent")

    class Meta:
        ordering = ["location", "size"]
        constraints = [models.UniqueConstraint(fields=["location", "size"], name="one_rent_per_location_and_size")]
        verbose_name = "default rent"

    def __str__(self):
        return f"{self.size} lockers in {self.location}: £{self.monthly_rent_pence / 100:.2f} a month"


class Locker(models.Model):
    class Hold(models.TextChoices):
        NONE = "", "Available to rent"
        RESERVED = "reserved", "Reserved for trustees"
        OUT_OF_USE = "out_of_use", "Out of use"

    location = models.ForeignKey(StorageLocation, on_delete=models.PROTECT, related_name="lockers")
    number = models.PositiveIntegerField()
    size = models.ForeignKey(LockerSize, on_delete=models.PROTECT, related_name="lockers")
    hold = models.CharField(
        "availability",
        max_length=20,
        choices=Hold.choices,
        blank=True,
        default=Hold.NONE,
        help_text="Only while it isn't rented. Reserved and out-of-use lockers aren't shown to members.",
    )
    hold_note = models.CharField(
        "availability note", max_length=255, blank=True, help_text="What for, or what's wrong."
    )
    advertised_rent_pence = models.PositiveIntegerField(
        "advertised rent override",
        null=True,
        blank=True,
        help_text="Instead of the default rent, for whoever rents it next (can be £0). Empty = the default.",
    )
    advertised_rent_reason = models.CharField(max_length=255, blank=True)
    retired = models.BooleanField(default=False, help_text="Removed from the space. Kept for its rental history.")

    class Meta:
        ordering = ["location", "number"]
        constraints = [
            models.UniqueConstraint(fields=["location", "number"], name="one_locker_per_location_and_number")
        ]

    def __str__(self):
        return f"{self.location} locker {self.number}"


class LockerRental(models.Model):
    """One member renting one locker, from start until it's given up."""

    locker = models.ForeignKey(Locker, on_delete=models.PROTECT, related_name="rentals")
    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="locker_rentals")
    started_at = models.DateTimeField(default=timezone.now)
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    rent_override_pence = models.PositiveIntegerField(
        "rent override",
        null=True,
        blank=True,
        help_text="What this member pays instead of the default (can be £0). Ends with the rental.",
    )
    rent_override_reason = models.CharField(max_length=255, blank=True)
    # Without an override the member pays the default rent, but a rise only reaches them after notice (06 §6.2):
    # `rent_pence` is the default they've agreed to, and a rise waits in `new_rent_pence` until `new_rent_from`.
    rent_pence = models.PositiveIntegerField(
        "agreed rent",
        null=True,
        blank=True,
        help_text="The default rent this member pays. Empty = the current default.",
    )
    new_rent_pence = models.PositiveIntegerField("new rent", null=True, blank=True)
    new_rent_from = models.DateField(
        null=True, blank=True, help_text="Charges on or after this date are for the new rent (after 30 days' notice)."
    )
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    end_reason = models.CharField(max_length=255, blank=True)
    # Rent is billed with the subscription on the member's payment day: apps/billing (Charge, kind locker-rent).

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["locker"], condition=Q(ended_at__isnull=True), name="one_open_rental_per_locker"
            )
        ]

    def __str__(self):
        return f"{self.locker} · {self.member}"

    @property
    def is_open(self):
        return self.ended_at is None


class MemberLockerLimit(models.Model):
    """A trustee's decision that one member may rent a different number of lockers from the usual limit."""

    member = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="locker_limit")
    limit = models.PositiveSmallIntegerField("lockers allowed")
    reason = models.CharField(max_length=255)
    set_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    set_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "member's locker limit"

    def __str__(self):
        return f"{self.member}: {self.limit} locker(s)"


class LockerChange(models.Model):
    """The audit log for storage: lockers' prices, availability, rentals, members' limits and box flags (06 §6.2)."""

    class What(models.TextChoices):
        DEFAULT_RENT = "default-rent", "Default rent"
        ADVERTISED_RENT = "advertised-rent", "Advertised rent override"
        RENTAL_RENT = "rental-rent", "Rental rent override"
        AVAILABILITY = "availability", "Availability"
        RENTED = "rented", "Rented"
        ENDED = "ended", "Rental ended"
        MEMBER_LIMIT = "member-limit", "Member's locker limit"
        BOX_FLAG = "box-flag", "Box paid flag"

    what = models.CharField(max_length=20, choices=What.choices)
    locker = models.ForeignKey(Locker, on_delete=models.CASCADE, null=True, blank=True, related_name="changes")
    rental = models.ForeignKey(LockerRental, on_delete=models.CASCADE, null=True, blank=True, related_name="changes")
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name="+"
    )
    description = models.CharField(max_length=255, help_text="What it applies to, e.g. 'Large lockers in Workshop'.")
    old_value = models.CharField(max_length=255, blank=True)
    new_value = models.CharField(max_length=255, blank=True)
    reason = models.CharField(max_length=255, blank=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    changed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-changed_at", "-pk"]

    def __str__(self):
        return f"{self.get_what_display()} · {self.description}: {self.old_value} → {self.new_value}"
