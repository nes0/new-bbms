from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm

from .models import Address, IdCheck, Member

# IdDocument is deliberately NOT registered: the uploads are only seen through the ID check pages, which log each
# view and delete the documents once checked (spec 03 §3.1).


class MemberCreationForm(UserCreationForm):
    class Meta:
        model = Member
        fields = ("email", "given_name", "family_name")


class MemberChangeForm(UserChangeForm):
    class Meta:
        model = Member
        fields = "__all__"


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    """Address changes waiting for approval (03 §3.9): filter by "approved: No" to see the queue."""

    list_display = ("member", "__str__", "approved", "created_at")
    list_filter = ("approved",)
    search_fields = ("member__email", "member__given_name", "member__family_name", "postcode")
    autocomplete_fields = ("member",)
    actions = ["approve", "decline"]

    @admin.action(description="Approve the selected addresses (replaces the member's old address)")
    def approve(self, request, queryset):
        count = 0
        for address in queryset.filter(approved=False):
            address.approve()
            count += 1
        self.message_user(request, f"Approved {count} address(es).")

    @admin.action(description="Decline the selected address changes (deletes them)")
    def decline(self, request, queryset):
        deleted, _ = queryset.filter(approved=False).delete()
        # TODO(notifications app): tell the member their address change wasn't accepted.
        self.message_user(request, f"Declined {deleted} address change(s).")


class AddressInline(admin.StackedInline):
    model = Address
    extra = 0
    fields = (("line_1", "line_2"), ("line_3", "line_4"), "postcode", "approved", "created_at")
    readonly_fields = ("created_at",)


@admin.register(Member)
class MemberAdmin(UserAdmin):
    form = MemberChangeForm
    add_form = MemberCreationForm
    inlines = [AddressInline]

    list_display = ("full_name", "email", "status", "trusted", "key_holder", "date_joined")
    list_filter = ("status", "trusted", "key_holder", "banned", "is_staff")
    search_fields = ("email", "given_name", "family_name")
    ordering = ("given_name", "family_name")
    readonly_fields = ("legacy_id", "date_joined", "updated_at", "last_login")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal details", {"fields": ("given_name", "family_name", "phone", "emergency_contact")}),
        (
            "Membership",
            {
                "fields": (
                    "status",
                    "monthly_subscription_pence",
                    "founder",
                    "induction_completed",
                    "rules_agreed_at",
                    "profile_private",
                )
            },
        ),
        ("Access", {"fields": ("trusted", "key_holder")}),
        ("Banned", {"classes": ("collapse",), "fields": ("banned", "banned_at", "banned_reason")}),
        ("Admin", {"fields": ("notes", "is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Record", {"classes": ("collapse",), "fields": ("legacy_id", "date_joined", "updated_at", "last_login")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "given_name", "family_name", "password1", "password2")}),
    )


@admin.register(IdCheck)
class IdCheckAdmin(admin.ModelAdmin):
    """The record that a member's ID was checked. Made from the ID check pages, so read-only here."""

    list_display = ("checked_at", "member", "result", "documents_seen", "checked_by")
    list_filter = ("result",)
    search_fields = ("member__email", "member__given_name", "member__family_name")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
