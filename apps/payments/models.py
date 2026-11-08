"""
The ledger (spec 02 §2.4, 04): one row per movement of money.

Rows are never edited to change money: a mistake or a change (for example an adjusted machine session) is put right
with a new, correcting row. All money is in pence.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone


class Payment(models.Model):
    class Reason(models.TextChoices):
        """What the money is for (04 §4.1)."""

        SUBSCRIPTION = "subscription", "Monthly subscription"
        INDUCTION = "induction", "Induction fee"
        KEY_DEPOSIT = "key-deposit", "Key deposit"
        STORAGE_BOX = "storage-box", "Box fee"
        LOCKER_RENT = "locker-rent", "Locker rent"
        BALANCE = "balance", "Credit top-up"
        EQUIPMENT_FEE = "equipment-fee", "Equipment use"
        WITHDRAWAL = "withdrawal", "Credit withdrawal"
        DONATION = "donation", "Donation"
        COSTS = "costs", "Costs"
        CONSUMABLE = "consumable", "Consumables"  # the reference names the service (e.g. a printer)
        TRANSFER = "transfer", "Transfer between members (legacy)"

    class Source(models.TextChoices):
        """How it was paid (04 §4.1)."""

        GOCARDLESS = "gocardless-variable", "Direct Debit"
        STRIPE = "stripe", "Card"
        BALANCE = "balance", "Member credit"
        CASH = "cash", "Cash"
        STANDING_ORDER = "standing-order", "Standing order"
        MANUAL = "manual", "Recorded by an admin"
        REIMBURSEMENT = "reimbursement", "Expense reimbursed"
        GOCARDLESS_OLD = "gocardless", "Direct Debit (old style, legacy)"
        PAYPAL = "paypal", "PayPal"
        TRANSFER = "transfer", "Transfer (legacy)"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        WITHDRAWN = "withdrawn", "Withdrawn"  # legacy status; counts towards credit, as it did there

    # Payments in these states count towards credit (the legacy rule, 04 §4.7).
    CREDIT_STATUSES = (Status.PAID, Status.PENDING, Status.WITHDRAWN)

    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payments",
        help_text="Empty for money not yet matched to a member (e.g. an unknown bank payment).",
    )
    reason = models.CharField(max_length=30, choices=Reason.choices)
    source = models.CharField(max_length=30, choices=Source.choices)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PAID)
    amount_pence = models.IntegerField(help_text="Negative only for a correction that gives money back.")
    fee_pence = models.PositiveIntegerField(default=0, help_text="Taken by the payment provider.")
    source_id = models.CharField(max_length=255, blank=True, help_text="The provider's id for this payment.")
    reference = models.CharField(
        max_length=255, blank=True, help_text="Context, e.g. the induction category or '<session id>:<machine>'."
    )
    note = models.TextField(blank=True)
    usage_session = models.ForeignKey(
        "equipment.UsageSession", on_delete=models.PROTECT, null=True, blank=True, related_name="payments"
    )
    charge = models.ForeignKey(
        "billing.Charge",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payments",
        help_text="The subscription or locker rent charge this pays.",
    )
    corrects = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="corrections",
        help_text="The earlier payment this one corrects.",
    )
    created_at = models.DateTimeField(default=timezone.now)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    legacy_id = models.PositiveIntegerField(null=True, blank=True, unique=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["member", "-created_at"]),
            models.Index(fields=["reason", "-created_at"]),
            models.Index(fields=["source", "source_id"]),
        ]

    def __str__(self):
        return f"{self.get_reason_display()} · £{self.amount_pence / 100:.2f} · {self.get_status_display()}"

    @property
    def amount_minus_fee_pence(self):
        return self.amount_pence - self.fee_pence

    @property
    def credit_effect_pence(self) -> int:
        """How this payment changes the member's credit: + for top-ups, − for spending from credit."""
        if self.status not in self.CREDIT_STATUSES:
            return 0
        effect = 0
        if self.reason == self.Reason.BALANCE:
            effect += self.amount_pence
        if self.source == self.Source.BALANCE:
            effect -= self.amount_pence
        return effect


class WithdrawalRequest(models.Model):
    """A member asking for credit to be paid out to their bank (04 §4.7). Seen only by finance and admins.

    The bank details are wiped once the request has been dealt with, so they're only kept as long as needed.
    """

    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        PAID = "paid", "Paid out"
        REJECTED = "rejected", "Rejected"

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="withdrawals")
    amount_pence = models.PositiveIntegerField()
    account_name = models.CharField(max_length=100, blank=True)
    sort_code = models.CharField(max_length=8, blank=True)
    account_number = models.CharField(max_length=8, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.REQUESTED)
    requested_at = models.DateTimeField(default=timezone.now)
    handled_at = models.DateTimeField(null=True, blank=True)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    payment = models.OneToOneField(Payment, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    note = models.CharField(max_length=255, blank=True, help_text="Why it was rejected, or anything else to note.")

    class Meta:
        ordering = ["-requested_at"]

    def __str__(self):
        return f"{self.member} · £{self.amount_pence / 100:.2f} · {self.get_status_display()}"


class CardCheckout(models.Model):
    """A member paying by card through Stripe Checkout (04 §4.3).

    The amount is worked out by BBMS when the checkout starts, never taken from the browser. The payment is
    recorded when Stripe says it's paid (its webhook, or the member's return to BBMS, whichever comes first).
    """

    class Purpose(models.TextChoices):
        TOP_UP = "top-up", "Credit top-up"
        INDUCTION = "induction", "Induction fee"
        KEY_DEPOSIT = "key-deposit", "Key deposit"
        STORAGE_BOX = "storage-box", "Box fee"
        CHARGE = "charge", "Subscription or locker rent"

    class Status(models.TextChoices):
        OPEN = "open", "Waiting for the member to pay"
        PAID = "paid", "Paid"
        EXPIRED = "expired", "Not paid (expired or abandoned)"

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="card_checkouts")
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    amount_pence = models.PositiveIntegerField()
    equipment = models.ForeignKey("equipment.Equipment", on_delete=models.PROTECT, null=True, blank=True)
    charge = models.ForeignKey("billing.Charge", on_delete=models.PROTECT, null=True, blank=True)
    session_id = models.CharField("Stripe Checkout session", max_length=255, unique=True, null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(default=timezone.now)
    paid_at = models.DateTimeField(null=True, blank=True)
    payment = models.OneToOneField(Payment, on_delete=models.PROTECT, null=True, blank=True, related_name="checkout")
    note = models.CharField(max_length=255, blank=True, help_text="E.g. why the money went to credit instead.")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_purpose_display()} £{self.amount_pence / 100:.2f} by card · {self.get_status_display()}"


class StripeEvent(models.Model):
    """A webhook event from Stripe, kept so a repeat is ignored and a failure can be looked at."""

    event_id = models.CharField(max_length=255, unique=True)
    type = models.CharField(max_length=100)
    object_id = models.CharField(max_length=255, blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=255, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.type} {self.object_id}"


class PayPalNotification(models.Model):
    """One IPN from PayPal (04 §4.4), kept so a repeat is ignored and a failure can be looked at.

    Only IPNs PayPal has confirmed are stored. `key` identifies the IPN: PayPal sends the same one again when it
    isn't sure BBMS got it, and sends a new one when a payment's status changes.
    """

    key = models.CharField(max_length=255, unique=True)
    txn_type = models.CharField(max_length=50, blank=True)
    txn_id = models.CharField(max_length=50, blank=True, db_index=True)
    payment_status = models.CharField(max_length=50, blank=True)
    subscr_id = models.CharField("subscription", max_length=50, blank=True)
    payer_email = models.CharField(max_length=255, blank=True)
    receiver_email = models.CharField(max_length=255, blank=True)
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    payment = models.ForeignKey(Payment, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    data = models.JSONField(help_text="Everything PayPal sent.")
    received_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=255, blank=True)
    error = models.TextField(blank=True, help_text="Why it wasn't handled. Fix the cause, then try it again.")

    class Meta:
        ordering = ["-received_at", "-pk"]
        verbose_name = "PayPal notification"

    def __str__(self):
        return f"{self.txn_type or 'IPN'} {self.txn_id or self.subscr_id} {self.payment_status}".strip()
