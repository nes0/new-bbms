from django import forms
from django.contrib import admin
from django.shortcuts import redirect
from django.urls import reverse

from .models import SettingChange, SiteSettings


class SiteSettingsForm(forms.ModelForm):
    """Money is stored in pence (01 §1.5) but entered in whole pounds."""

    minimum_subscription_pounds = forms.IntegerField(
        label="Minimum monthly subscription (£)",
        min_value=1,
        max_value=1000,
        help_text=SiteSettings._meta.get_field("minimum_subscription_pence").help_text,
    )

    class Meta:
        model = SiteSettings
        fields = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["minimum_subscription_pounds"].initial = self.instance.minimum_subscription_pounds

    def save(self, commit=True):
        self.instance.minimum_subscription_pence = self.cleaned_data["minimum_subscription_pounds"] * 100
        return super().save(commit)


def _display(settings_row, name):
    value = getattr(settings_row, name)
    return f"£{value / 100:.2f}" if name.endswith("_pence") else str(value)


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    form = SiteSettingsForm
    fieldsets = ((None, {"fields": ("minimum_subscription_pounds",)}),)

    # One row only: the list goes straight to it, and it can't be added twice or deleted.
    def changelist_view(self, request, extra_context=None):
        settings_row = SiteSettings.load()
        return redirect(reverse("admin:site_settings_sitesettings_change", args=[settings_row.pk]))

    def get_object(self, request, object_id, from_field=None):
        return SiteSettings.load()  # creates the row with its defaults the first time

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        before = SiteSettings.objects.filter(pk=obj.pk).first()
        super().save_model(request, obj, form, change)
        for field in SiteSettings._meta.concrete_fields:
            if field.primary_key or before is None:
                continue
            old, new = _display(before, field.name), _display(obj, field.name)
            if old != new:
                SettingChange.objects.create(
                    setting=str(field.verbose_name), old_value=old, new_value=new, changed_by=request.user
                )


@admin.register(SettingChange)
class SettingChangeAdmin(admin.ModelAdmin):
    list_display = ("changed_at", "setting", "old_value", "new_value", "changed_by")
    list_filter = ("setting",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
