from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm

from .models import Member


class MemberCreationForm(UserCreationForm):
    class Meta:
        model = Member
        fields = ("email", "given_name", "family_name")


class MemberChangeForm(UserChangeForm):
    class Meta:
        model = Member
        fields = "__all__"


@admin.register(Member)
class MemberAdmin(UserAdmin):
    form = MemberChangeForm
    add_form = MemberCreationForm

    list_display = ("full_name", "email", "status", "trusted", "key_holder", "date_joined")
    list_filter = ("status", "trusted", "key_holder", "banned", "is_staff")
    search_fields = ("email", "given_name", "family_name")
    ordering = ("given_name", "family_name")
    readonly_fields = ("legacy_id", "date_joined", "updated_at", "last_login")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal details", {"fields": ("given_name", "family_name", "phone", "emergency_contact")}),
        ("Membership", {"fields": ("status", "founder", "induction_completed", "rules_agreed_at", "profile_private")}),
        ("Access", {"fields": ("trusted", "key_holder")}),
        ("Banned", {"classes": ("collapse",), "fields": ("banned", "banned_at", "banned_reason")}),
        ("Admin", {"fields": ("notes", "is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Record", {"classes": ("collapse",), "fields": ("legacy_id", "date_joined", "updated_at", "last_login")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "given_name", "family_name", "password1", "password2")}),
    )
