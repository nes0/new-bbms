"""
Expenses: members claiming back what they spent for the space (spec 06 §6.4). The rules are in `claims.py`.

An approved expense is paid into the member's credit (a ledger row with source "reimbursement"), which they can spend
or ask to withdraw. Receipts are private files, served only through BBMS to the member and to those who handle
expenses.
"""

import uuid
from pathlib import PurePosixPath

from django.conf import settings
from django.db import models
from django.utils import timezone


def _receipt_path(expense, filename):
    # A random name, so a receipt's address says nothing about whose it is.
    return f"expense-receipts/{uuid.uuid4().hex}{PurePosixPath(filename).suffix.lower()}"


class Expense(models.Model):
    class Category(models.TextChoices):
        # Legacy's list, with "other" added.
        CONSUMABLES = "consumables", "Consumables"
        FOOD = "food", "Food"
        EQUIPMENT_REPAIR = "equipment-repair", "Equipment repair"
        TOOLS = "tools", "Tools"
        INFRASTRUCTURE = "infrastructure", "Infrastructure"
        PROMOTION = "promotion", "Promotional materials"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        WAITING = "waiting", "Waiting for approval"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="expenses")
    category = models.CharField(max_length=30, choices=Category.choices)
    description = models.TextField(max_length=1000, help_text="What you bought, and what it's for.")
    amount_pence = models.PositiveIntegerField()
    spent_on = models.DateField("date of purchase")
    receipt = models.FileField(upload_to=_receipt_path, blank=True)
    legacy_receipt = models.CharField(
        max_length=255, blank=True, help_text="Where legacy kept the receipt, if it wasn't copied over."
    )
    submitted_at = models.DateTimeField(default=timezone.now)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.WAITING)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decline_reason = models.TextField(blank=True, max_length=1000)
    payment = models.OneToOneField(
        "payments.Payment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="expense",
        help_text="The credit the member was paid.",
    )
    legacy_id = models.PositiveIntegerField(null=True, blank=True, unique=True)

    class Meta:
        ordering = ["-submitted_at", "-pk"]
        permissions = [
            ("approve_expense", "Can approve or decline expenses"),
            ("see_all_expenses", "Can see everyone's expenses and receipts"),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount_pence__gt=0), name="an_expense_is_for_more_than_nothing")
        ]

    def __str__(self):
        return f"Expense {self.pk}: £{self.amount_pence / 100:.2f}"

    @property
    def receipt_is_pdf(self) -> bool:
        return self.receipt.name.lower().endswith(".pdf")
