"""
The ledger in the admin (spec 04 §4.5, §4.8).

Payments are read-only once recorded: money is only ever put right with a new row. New cash and manual payments are
recorded through "Record a payment", which works out fixed prices itself.
"""

from django.contrib import admin, messages
from django.db.models import Sum
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from . import services
from .credit import credit_pence
from .forms import RecordPaymentForm
from .models import Payment, WithdrawalRequest
from .templatetags.money import pounds


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("created_at", "member", "reason", "source", "amount_display", "status", "reference")
    list_filter = ("reason", "source", "status", "created_at")
    search_fields = ("member__email", "member__given_name", "member__family_name", "reference", "source_id")
    date_hierarchy = "created_at"
    list_select_related = ("member",)
    readonly_fields = [f.name for f in Payment._meta.fields if f.name != "note"]
    change_list_template = "admin/payments/payment/change_list.html"

    @admin.display(description="Amount", ordering="amount_pence")
    def amount_display(self, payment):
        return pounds(payment.amount_pence)

    def has_add_permission(self, request):
        return False  # use "Record a payment" instead, which works out prices on the server

    def has_delete_permission(self, request, obj=None):
        # Only cash put into or taken out of credit by an admin, to undo a mistake (04 §4.8).
        if obj is None:
            return super().has_delete_permission(request)
        is_cash_credit = (obj.source == Payment.Source.CASH and obj.reason == Payment.Reason.BALANCE) or (
            obj.reason == Payment.Reason.WITHDRAWAL and obj.source == Payment.Source.BALANCE
        )
        return is_cash_credit and super().has_delete_permission(request, obj)

    def get_urls(self):
        record = path("record/", self.admin_site.admin_view(self.record_view), name="payments_payment_record")
        return [record, *super().get_urls()]

    def record_view(self, request):
        if not self.has_view_or_change_permission(request) or not request.user.has_perm("payments.add_payment"):
            return redirect("admin:index")
        form = RecordPaymentForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            try:
                payment = form.save(created_by=request.user)
            except services.InsufficientCredit as error:
                form.add_error(None, str(error))
            except ValueError as error:
                form.add_error(None, str(error))
            else:
                member = payment.member
                messages.success(
                    request, f"Recorded {payment} for {member}. Their credit is now {pounds(credit_pence(member))}."
                )
                return redirect(reverse("admin:payments_payment_changelist") + f"?member__id__exact={member.pk}")
        context = {
            **self.admin_site.each_context(request),
            "title": "Record a payment",
            "form": form,
            "opts": self.model._meta,
        }
        return TemplateResponse(request, "admin/payments/payment/record.html", context)

    def changelist_view(self, request, extra_context=None):
        response = super().changelist_view(request, extra_context)
        changelist = getattr(response, "context_data", {}).get("cl")
        if changelist is not None:
            totals = changelist.queryset.aggregate(amount=Sum("amount_pence"), fees=Sum("fee_pence"))
            response.context_data["totals"] = {key: pounds(value or 0) for key, value in totals.items()}
            response.context_data["can_record"] = request.user.has_perm("payments.add_payment")
        return response


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(admin.ModelAdmin):
    list_display = ("requested_at", "member", "amount_display", "credit_now", "status", "handled_at")
    list_filter = ("status",)
    search_fields = ("member__email", "member__given_name", "member__family_name")
    readonly_fields = [f.name for f in WithdrawalRequest._meta.fields if f.name != "note"]
    actions = ["mark_paid", "reject"]

    @admin.display(description="Amount", ordering="amount_pence")
    def amount_display(self, withdrawal):
        return pounds(withdrawal.amount_pence)

    @admin.display(description="Their credit now")
    def credit_now(self, withdrawal):
        return pounds(credit_pence(withdrawal.member))

    def has_add_permission(self, request):
        return False  # members ask for withdrawals from their credit page

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Mark as paid (takes the amount from their credit)")
    def mark_paid(self, request, queryset):
        for withdrawal in queryset.filter(status=WithdrawalRequest.Status.REQUESTED):
            try:
                services.pay_withdrawal(withdrawal, request.user)
            except services.InsufficientCredit as error:
                messages.error(request, f"{withdrawal.member}: {error} Not paid.")
            else:
                messages.success(request, f"{withdrawal.member}: {pounds(withdrawal.amount_pence)} paid out.")

    @admin.action(description="Reject")
    def reject(self, request, queryset):
        for withdrawal in queryset.filter(status=WithdrawalRequest.Status.REQUESTED):
            services.reject_withdrawal(withdrawal, request.user)
            messages.info(request, f"{withdrawal.member}: rejected. Add a note to say why.")
