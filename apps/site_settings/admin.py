from decimal import Decimal

from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.shortcuts import redirect
from django.urls import reverse

from .models import SettingChange, SiteSettings

PENCE_FIELDS = [f.name for f in SiteSettings._meta.concrete_fields if f.name.endswith("_pence")]
OTHER_FIELDS = [f.name for f in SiteSettings._meta.concrete_fields if not f.primary_key and f.name not in PENCE_FIELDS]


def _pounds_name(pence_name):
    return pence_name.removesuffix("_pence") + "_pounds"


class SiteSettingsForm(forms.ModelForm):
    """Money is stored in pence (01 §1.5) but entered in pounds; everything else is the model's own field."""

    class Meta:
        model = SiteSettings
        fields = OTHER_FIELDS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in PENCE_FIELDS:
            model_field = SiteSettings._meta.get_field(name)
            self.fields[_pounds_name(name)] = forms.DecimalField(
                label=f"{model_field.verbose_name[:1].upper()}{model_field.verbose_name[1:]} (£)",
                min_value=Decimal("0"),
                max_digits=8,
                decimal_places=2,
                help_text=model_field.help_text,
                initial=(Decimal(getattr(self.instance, name)) / 100).quantize(Decimal("0.01")),
            )

    def clean(self):
        cleaned = super().clean()
        for name in PENCE_FIELDS:
            pounds = cleaned.get(_pounds_name(name))
            if pounds is None:
                continue
            pence = int(pounds * 100)
            try:
                # The model's own rules (minimums, whole pounds) apply to money fields too.
                SiteSettings._meta.get_field(name).run_validators(pence)
            except ValidationError as error:
                self.add_error(_pounds_name(name), error)
                continue
            setattr(self.instance, name, pence)
        return cleaned


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    form = SiteSettingsForm
    fieldsets = [
        (title, {"fields": [_pounds_name(n) if n in PENCE_FIELDS else n for n in names]})
        for title, names in SiteSettings.GROUPS
    ]

    def get_form(self, request, obj=None, **kwargs):
        # The pound fields are added by the form itself; the admin only needs the model's own fields.
        kwargs["fields"] = OTHER_FIELDS
        return super().get_form(request, obj, **kwargs)

    # One row only: the list goes straight to it, and it can't be added twice or deleted.
    def changelist_view(self, request, extra_context=None):
        return redirect(reverse("admin:site_settings_sitesettings_change", args=[SiteSettings.load().pk]))

    def get_object(self, request, object_id, from_field=None):
        return SiteSettings.load()  # creates the row with its defaults the first time

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        before = SiteSettings.objects.filter(pk=obj.pk).first()
        super().save_model(request, obj, form, change)
        if before is None:
            return
        for field in SiteSettings._meta.concrete_fields:
            if field.primary_key:
                continue
            old, new = before.display(field.name), obj.display(field.name)
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
