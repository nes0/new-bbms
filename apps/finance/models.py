"""
Bank statements imported by finance (spec 04 §4.6, §4.9), and the remembered matches between bank descriptions and
members. How a statement is read and matched is in `statements.py`.

A statement is uploaded as a **draft**: BBMS reads its credits and suggests a member and a reason for each, and
nothing is recorded until finance has checked them and pressed "Record". Every credit line is then kept, matched or
not, so differences with the bank are listed rather than hidden.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class BankStatement(models.Model):
    filename = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    uploaded_at = models.DateTimeField(default=timezone.now)
    skipped_lines = models.PositiveIntegerField(default=0, help_text="Money going out, and lines that aren't money.")
    recorded_at = models.DateTimeField(null=True, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-uploaded_at", "-pk"]
        permissions = [
            ("see_finance", "Can see the finance pages (payments, charges, bank lines)"),
            ("manage_finance", "Can import bank statements and put payments right"),
        ]

    def __str__(self):
        return f"{self.filename} ({self.uploaded_at:%d %b %Y})"

    @property
    def is_draft(self):
        return self.recorded_at is None


class BankLine(models.Model):
    """One credit (money in) on a bank statement."""

    class Kind(models.TextChoices):
        MEMBER = "member", "From a member (standing order or transfer)"
        GOCARDLESS = "gocardless", "GoCardless payout"
        STRIPE = "stripe", "Stripe payout"
        PAYPAL = "paypal", "PayPal transfer"
        OTHER = "other", "Other income (e.g. Eventbrite)"

    class Outcome(models.TextChoices):
        WAITING = "", "Not recorded yet"
        RECORDED = "recorded", "Recorded as a payment"
        PAYOUT = "payout", "A payout (made up of payments already in BBMS)"
        NOT_MEMBER = "not-member", "Not a member payment"
        UNMATCHED = "unmatched", "Not matched yet"
        DUPLICATE = "duplicate", "Already imported"

    class Reason(models.TextChoices):
        SUBSCRIPTION = "subscription", "Subscription"
        BALANCE = "balance", "Credit top-up"

    statement = models.ForeignKey(BankStatement, on_delete=models.CASCADE, related_name="lines")
    row = models.PositiveIntegerField(help_text="Its row in the file.")
    date = models.DateField()
    description = models.CharField(max_length=500)
    amount_pence = models.PositiveIntegerField()
    kind = models.CharField(max_length=20, choices=Kind.choices)
    fingerprint = models.CharField(max_length=600, help_text="Date, amount and description: spots a line seen before.")
    kept = models.BooleanField(default=False, help_text="Part of a recorded statement.")

    # What BBMS suggested when the statement was read.
    suggested_member = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    suggested_reason = models.CharField(max_length=20, choices=Reason.choices, blank=True)
    how_matched = models.CharField(max_length=255, blank=True)
    reference = models.CharField(max_length=100, blank=True, help_text="The reference after -BALANCE-, if any.")

    # What finance decided.
    outcome = models.CharField(max_length=20, choices=Outcome.choices, blank=True, default=Outcome.WAITING)
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="bank_lines"
    )
    payment = models.OneToOneField(
        "payments.Payment", on_delete=models.PROTECT, null=True, blank=True, related_name="bank_line"
    )
    note = models.CharField(max_length=255, blank=True)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    handled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["date", "row"]
        constraints = [
            models.UniqueConstraint(
                fields=["fingerprint"], condition=Q(kept=True), name="a_bank_line_is_recorded_once"
            ),
        ]

    def __str__(self):
        return f"{self.date:%d/%m/%Y} £{self.amount_pence / 100:.2f} {self.description}"


class BankMatch(models.Model):
    """Text that identifies a member on the bank statement: a whole description finance has confirmed before, or a
    piece of one (legacy's "import match string")."""

    text = models.CharField(max_length=500, unique=True, help_text="In capitals, spaces tidied.")
    whole = models.BooleanField(
        default=True, help_text="Ticked: the whole description must be this. Unticked: it must contain this."
    )
    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bank_matches")
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["text"]
        verbose_name_plural = "bank matches"

    def __str__(self):
        return f"{self.text} → {self.member}"
