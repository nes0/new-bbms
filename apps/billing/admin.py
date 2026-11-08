"""
Charges, Direct Debit set-ups and GoCardless events in the admin (spec 03 §3.3, 04 §4.2, §4.8).

Charges are made and changed by the billing jobs and the webhook, so they're read-only here, with actions for the
things a trustee may need to do by hand: retry a failed Direct Debit, or cancel a charge.
"""

from django.contrib import admin, messages
from django.db import transaction

from apps.members.admin import MemberAdmin
from apps.payments.models import Payment
from apps.payments.templatetags.money import pounds

from . import gocardless
from .billing import locked_rows
from .models import Charge, GoCardlessEvent, MandateSetup
from .webhooks import process_pending


class ChargeInline(admin.TabularInline):
    model = Charge
    fk_name = "member"
    fields = ("charge_date", "kind", "rental", "amount_display", "status", "paid_on", "retry_on", "failure_cause")
    readonly_fields = fields
    extra = 0
    max_num = 0
    can_delete = False
    show_change_link = True
    ordering = ("-charge_date",)
    verbose_name_plural = "Charges (newest first)"

    @admin.display(description="Amount")
    def amount_display(self, charge):
        return pounds(charge.amount_pence) if charge.amount_pence else "—"

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("rental__locker")


MemberAdmin.inlines = [*MemberAdmin.inlines, ChargeInline]


@admin.register(Charge)
class ChargeAdmin(admin.ModelAdmin):
    list_display = ("charge_date", "member", "kind", "amount_display", "status", "paid_on", "retry_on")
    list_filter = ("status", "kind", "charge_date")
    search_fields = ("member__email", "member__given_name", "member__family_name")
    date_hierarchy = "charge_date"
    list_select_related = ("member", "rental__locker")
    readonly_fields = [f.name for f in Charge._meta.fields]
    actions = ["retry_direct_debit", "cancel"]

    @admin.display(description="Amount", ordering="amount_pence")
    def amount_display(self, charge):
        return pounds(charge.amount_pence) if charge.amount_pence else "—"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Ask GoCardless to retry the failed Direct Debit", permissions=["change"])
    def retry_direct_debit(self, request, queryset):
        payment_ids = set(
            Payment.objects.filter(
                charge__in=queryset.filter(status__in=(Charge.Status.FAILED, Charge.Status.PROCESSING)),
                source=Payment.Source.GOCARDLESS,
                status=Payment.Status.FAILED,
            ).values_list("source_id", flat=True)
        )
        if not payment_ids:
            self.message_user(request, "None of those has a failed Direct Debit to retry.", messages.WARNING)
            return
        for payment_id in payment_ids:
            try:
                with transaction.atomic():
                    rows = locked_rows(payment_id)
                    gocardless.client().retry_payment(payment_id)
                    rows.update(status=Payment.Status.PENDING)
                    Charge.objects.filter(payments__in=rows).update(status=Charge.Status.PROCESSING, retry_on=None)
                self.message_user(request, f"Asked GoCardless to retry {payment_id}.")
            except gocardless.GoCardlessError as error:
                self.message_user(request, str(error), messages.ERROR)

    @admin.action(description="Cancel the selected charges (nothing is owed)", permissions=["change"])
    def cancel(self, request, queryset):
        count = queryset.filter(status__in=(Charge.Status.PENDING, Charge.Status.DUE, Charge.Status.FAILED)).update(
            status=Charge.Status.CANCELLED
        )
        self.message_user(request, f"Cancelled {count} charge(s). Processing and paid charges were left alone.")


@admin.register(MandateSetup)
class MandateSetupAdmin(admin.ModelAdmin):
    list_display = ("started_at", "member", "completed_at", "mandate_id")
    search_fields = ("member__email", "member__given_name", "member__family_name", "mandate_id")
    readonly_fields = [f.name for f in MandateSetup._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(GoCardlessEvent)
class GoCardlessEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "resource_type", "action", "resource_id", "outcome", "has_error")
    list_filter = ("resource_type", "action", ("processed_at", admin.EmptyFieldListFilter))
    search_fields = ("event_id", "resource_id")
    readonly_fields = [f.name for f in GoCardlessEvent._meta.fields]
    actions = ["process_again"]

    @admin.display(description="Failed", boolean=True)
    def has_error(self, event):
        return bool(event.error)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Try failed events again", permissions=["change"])
    def process_again(self, request, queryset):
        count = process_pending(retry_failed=True)
        self.message_user(request, f"Handled {count} event(s).")
