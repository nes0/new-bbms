from django.contrib import admin

from .models import VouchChange


@admin.register(VouchChange)
class VouchChangeAdmin(admin.ModelAdmin):
    """The vouching history, read only (05 §5.10). Each member's admin page shows theirs too."""

    list_display = ("at", "member", "voucher", "action", "note")
    list_filter = ("action",)
    search_fields = ("member__given_name", "member__family_name", "voucher__given_name", "voucher__family_name")
    list_select_related = ("member", "voucher")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
