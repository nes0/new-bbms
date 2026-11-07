"""Admin forms for issuing fobs (spec 05 §5.1)."""

from django import forms
from django.contrib.admin.widgets import AutocompleteSelect
from django.contrib.auth import get_user_model

from . import fobs
from .models import Device, KeyFob


def _member_field(admin_site):
    return forms.ModelChoiceField(
        queryset=get_user_model().objects.all(),
        widget=AutocompleteSelect(KeyFob._meta.get_field("member"), admin_site),
    )


class IssueFobForm(forms.Form):
    fob_id = forms.CharField(
        label="Fob",
        max_length=20,
        help_text="Tap the fob on the USB reader (hex mode), or type the 12-character tag or the number printed on "
        "the fob.",
        widget=forms.TextInput(attrs={"autofocus": True, "autocomplete": "off"}),
    )

    def __init__(self, *args, admin_site, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields = {"member": _member_field(admin_site), **self.fields}

    def clean(self):
        data = super().clean()
        member, fob_id = data.get("member"), data.get("fob_id")
        if member is None or not fob_id:
            return data
        try:
            data["tag_id"], data["card_number"] = fobs.parse_fob_id(fob_id)
        except fobs.FobError as error:
            self.add_error("fob_id", str(error))
            return data
        reason = fobs.can_have_fob_reason(member)
        if reason:
            raise forms.ValidationError(reason)
        reason = fobs.already_registered_reason(data["tag_id"], data["card_number"])
        if reason:
            self.add_error("fob_id", reason)
        return data


class OpenEnrolmentForm(forms.Form):
    device = forms.ModelChoiceField(
        label="Reader",
        queryset=Device.objects.filter(enabled=True).order_by("role", "name"),
        help_text="Where you'll tap the new fob. The front door is usually easiest.",
    )

    def __init__(self, *args, admin_site, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields = {"member": _member_field(admin_site), **self.fields}
        door = Device.objects.filter(enabled=True, role=Device.Role.ENTRY).order_by("name").first()
        if door is not None:
            self.fields["device"].initial = door.pk
