from django.contrib import admin, messages

from apps.members.admin import MemberAdmin

from . import tags
from .models import AccessEvent, Device, KeyFob


class KeyFobInline(admin.TabularInline):
    model = KeyFob
    fk_name = "member"
    extra = 0
    fields = ("tag_id", "card_number", "active", "issued_at", "lost_at")
    readonly_fields = ("issued_at", "lost_at")
    show_change_link = True


# Show a member's fobs on their admin page.
MemberAdmin.inlines = [*MemberAdmin.inlines, KeyFobInline]


@admin.register(KeyFob)
class KeyFobAdmin(admin.ModelAdmin):
    list_display = ("__str__", "card_number_display", "member", "active", "issued_at")
    list_filter = ("active",)
    search_fields = ("tag_id", "member__email", "member__given_name", "member__family_name")
    autocomplete_fields = ("member", "issued_by")
    readonly_fields = ("lost_at", "legacy_id")
    actions = ["mark_lost"]

    @admin.display(description="Card number")
    def card_number_display(self, fob):
        return tags.format_card_number(fob.card_number) if fob.card_number is not None else "—"

    @admin.action(description="Mark selected fobs as lost")
    def mark_lost(self, request, queryset):
        for fob in queryset.filter(active=True):
            fob.mark_lost()
        self.message_user(request, "Marked as lost.")

    def save_model(self, request, obj, form, change):
        if not change and obj.issued_by is None:
            obj.issued_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("name", "device_id", "role", "last_heartbeat", "monitor_heartbeat")
    list_filter = ("role", "monitor_heartbeat")
    readonly_fields = ("last_boot", "last_heartbeat", "legacy_id")
    actions = ["new_api_key"]

    @admin.action(description="Generate a new API key (the old one stops working)")
    def new_api_key(self, request, queryset):
        for device in queryset:
            key = device.generate_api_key()
            device.save(update_fields=["api_key_hash"])
            # Shown once only: BBMS keeps just a hash of the key.
            self.message_user(request, f"New API key for {device.device_id}: {key}", messages.WARNING)

    def save_model(self, request, obj, form, change):
        if not change:
            key = obj.generate_api_key()
            message = f"API key for {obj.device_id} (shown once, copy it now): {key}"
            self.message_user(request, message, messages.WARNING)
        super().save_model(request, obj, form, change)


@admin.register(AccessEvent)
class AccessEventAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "kind", "member", "device", "reason", "delayed")
    list_filter = ("kind", "device", "delayed")
    search_fields = ("member__email", "member__given_name", "member__family_name")
    date_hierarchy = "occurred_at"

    # The log is a record of what happened: read-only.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
