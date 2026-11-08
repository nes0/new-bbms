from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm

from . import changes
from .models import Address, IdCheck, Member, MemberChange

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

    def clean(self):
        cleaned = super().clean()
        # A key holder must be trusted and have had their general induction confirmed (02 §2.2, 05 §5.9); the door
        # also needs a photo (05 §5.2). Checked when key holder is ticked, so existing key holders can still be edited.
        if cleaned.get("key_holder") and "key_holder" in self.changed_data:
            if not cleaned.get("trusted"):
                self.add_error("key_holder", "Only a trusted member can be a key holder: tick Trusted first.")
            elif self.instance.pk and not self.instance.induction_confirmed:
                self.add_error(
                    "key_holder",
                    "Their general induction must be confirmed first (Inductions page) before they get a key.",
                )
        return cleaned

    def clean_monthly_subscription_pence(self):
        """Admins may go below the usual minimum, down to the admin minimum (03 §3.7)."""
        from apps.site_settings.models import SiteSettings

        pence = self.cleaned_data.get("monthly_subscription_pence")
        minimum = SiteSettings.load().admin_minimum_subscription_pence
        if pence is not None and pence < minimum:
            raise forms.ValidationError(f"At least {minimum}p (£{minimum // 100}).")
        return pence


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
    """Ticking "approved" on a waiting address makes it the member's address (the old one is deleted); deleting a
    waiting one declines it (03 §3.9).
    """

    model = Address
    extra = 0
    fields = (("line_1", "line_2"), ("line_3", "line_4"), "postcode", "approved", "created_at")
    readonly_fields = ("created_at",)


class MemberChangeInline(admin.TabularInline):
    """The member's audit log (03 §3.9). Written by apps/members/changes.py, so read-only."""

    model = MemberChange
    fk_name = "member"
    extra = 0
    fields = ("changed_at", "field", "old_value", "new_value", "changed_by")
    readonly_fields = fields
    can_delete = False
    verbose_name_plural = "Audit log"

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class PhotoFilter(admin.SimpleListFilter):
    """Includes the "trusted members missing photos" report (03 §3.9): trusted members with neither an approved
    photo nor one on the wall, so a key would not open the door for them (05 §5.2).
    """

    title = "photo"
    parameter_name = "photo"

    def lookups(self, request, model_admin):
        return [
            ("trusted-missing", "Trusted, with no photo"),
            ("approved", "Approved photo"),
            ("wall", "Photo on the wall"),
            ("none", "No photo"),
        ]

    def queryset(self, request, queryset):
        no_photo = queryset.filter(photo_approved=False, photo_on_wall=False)
        return {
            "trusted-missing": no_photo.filter(trusted=True).exclude(status=Member.Status.LEFT),
            "approved": queryset.filter(photo_approved=True),
            "wall": queryset.filter(photo_on_wall=True),
            "none": no_photo,
        }.get(self.value(), queryset)


@admin.register(Member)
class MemberAdmin(UserAdmin):
    form = MemberChangeForm
    add_form = MemberCreationForm
    inlines = [AddressInline]  # other apps add theirs (fobs, lockers, charges); the audit log goes last, below
    change_form_template = "admin/members/member/change_form.html"

    list_display = ("full_name", "email", "status", "payment_method", "subscription_expires", "date_joined")
    list_filter = ("status", "payment_method", "trusted", "key_holder", PhotoFilter, "banned", "is_staff")
    search_fields = ("email", "given_name", "family_name")
    ordering = ("given_name", "family_name")
    # The mandate comes from GoCardless; a status change by hand doesn't send the emails or cancel charges, so the
    # billing jobs and the member's own pages are the usual way to change it.
    readonly_fields = (
        "legacy_id",
        "date_joined",
        "updated_at",
        "last_login",
        "mandate_id",
        "paypal_subscription_id",
        "discord_user_id",
        "discord_username",
        "discord_on_server",
        "discord_welcome_due",
        "suspended_at",
        "inducted_by",
        "inducted_at",
    )

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal details", {"fields": ("given_name", "family_name", "phone", "emergency_contact")}),
        (
            "Membership",
            {
                "fields": (
                    "status",
                    "monthly_subscription_pence",
                    "payment_method",
                    "payment_day",
                    "subscription_expires",
                    "mandate_id",
                    "paypal_email",
                    "paypal_subscription_id",
                    "discord_username",
                    "discord_user_id",
                    "discord_on_server",
                    "discord_welcome_due",
                    "discord_exempt",
                    "suspended_at",
                    "founder",
                    "induction_completed",
                    "inducted_by",
                    "inducted_at",
                    "rules_agreed_at",
                    "profile_private",
                )
            },
        ),
        (
            "Access",
            {
                "description": "Changes here are written to the audit log at the bottom of the page. Making someone "
                "trusted emails them.",
                "fields": ("trusted", "key_holder", "photo_approved", "photo_on_wall"),
            },
        ),
        ("Banned", {"classes": ("collapse",), "fields": ("banned", "banned_at", "banned_reason")}),
        ("Admin", {"fields": ("notes", "is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Record", {"classes": ("collapse",), "fields": ("legacy_id", "date_joined", "updated_at", "last_login")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "given_name", "family_name", "password1", "password2")}),
    )

    def get_inlines(self, request, obj):
        return [*super().get_inlines(request, obj), *([MemberChangeInline] if obj else [])]

    def save_model(self, request, obj, form, change):
        before = changes.snapshot(Member.objects.get(pk=obj.pk)) if change else None
        super().save_model(request, obj, form, change)
        if before is not None:
            changes.record(obj, before, changed_by=request.user)

    def save_formset(self, request, form, formset, change):
        if formset.model is not Address:
            return super().save_formset(request, form, formset, change)
        super().save_formset(request, form, formset, change)
        for address_form in formset.forms:
            address = address_form.instance
            ticked = "approved" in address_form.changed_data and address.approved
            if ticked and address.pk and address_form not in formset.deleted_forms:
                address.approve()  # deletes the out-of-date addresses


@admin.register(MemberChange)
class MemberChangeAdmin(admin.ModelAdmin):
    """The audit log for all members. Read-only: it's written by the changes it records."""

    list_display = ("changed_at", "member", "field", "old_value", "new_value", "changed_by")
    list_filter = ("field",)
    search_fields = ("member__email", "member__given_name", "member__family_name")
    date_hierarchy = "changed_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
