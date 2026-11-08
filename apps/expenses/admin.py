"""Expenses in the admin: a read-only record. Claims are approved or declined on the expense's own page
(`/expenses/<id>/`), which shows the receipt and tells the member.
"""

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import Expense


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ["submitted_at", "member", "category", "amount", "status", "decided_by"]
    list_filter = ["status", "category"]
    search_fields = ["member__given_name", "member__family_name", "member__email", "description"]
    date_hierarchy = "submitted_at"
    list_select_related = ["member", "decided_by"]
    readonly_fields = [
        "page",
        "member",
        "category",
        "description",
        "amount",
        "spent_on",
        "receipt",
        "legacy_receipt",
        "submitted_at",
        "status",
        "decided_by",
        "decided_at",
        "decline_reason",
        "payment",
        "legacy_id",
    ]
    exclude = ["amount_pence"]

    @admin.display(description="amount", ordering="amount_pence")
    def amount(self, obj):
        return f"£{obj.amount_pence / 100:,.2f}"

    @admin.display(description="decide, and see the receipt")
    def page(self, obj):
        return format_html('<a href="{}">On the expense\'s page</a>', reverse("expenses:detail", args=[obj.pk]))

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
