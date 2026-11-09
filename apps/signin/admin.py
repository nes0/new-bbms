"""Two-factor logins in the admin: only to see who has it on, and to switch it off for a member who has lost their
phone and recovery codes (after checking who's asking)."""

from django.contrib import admin

from . import two_factor
from .models import TwoFactor


@admin.register(TwoFactor)
class TwoFactorAdmin(admin.ModelAdmin):
    list_display = ("member", "confirmed_at", "codes_left")
    search_fields = ("member__email", "member__given_name", "member__family_name")
    fields = ("member", "created_at", "confirmed_at")
    readonly_fields = fields
    actions = ["switch_off"]

    @admin.display(description="recovery codes left")
    def codes_left(self, obj):
        return two_factor.recovery_codes_left(obj.member)

    @admin.action(description="Switch off two-factor login for the selected members")
    def switch_off(self, request, queryset):
        members = [device.member for device in queryset.select_related("member")]
        for member in members:
            two_factor.turn_off(member)
        self.message_user(request, f"Two-factor login switched off for {len(members)} member(s).")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)  # "switch off" also removes the recovery codes
        return actions

    def has_delete_permission(self, request, obj=None):
        return False
