"""Forms on the member's own account page (spec 03 §3.7–3.8, 02 §2.3)."""

from django import forms
from django.core.validators import MinLengthValidator

from .forms import normalise_postcode, subscription_field
from .models import Address, Member


class DetailsForm(forms.ModelForm):
    """Name, phone and emergency contact. The email address is the login, so it isn't changed here."""

    class Meta:
        model = Member
        fields = ["given_name", "family_name", "phone", "emergency_contact"]
        labels = {"phone": "Phone number"}
        help_texts = {"emergency_contact": "Who we should contact in an emergency, and how (name and phone number)."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Required for members, as at registration (02 §2.2); only admins may leave them empty.
        for name in self.fields:
            self.fields[name].required = True
        self.fields["phone"].validators.append(MinLengthValidator(10))

    def clean(self):
        cleaned = super().clean()
        for name in ("given_name", "family_name", "phone", "emergency_contact"):
            if isinstance(cleaned.get(name), str):
                cleaned[name] = cleaned[name].strip()
        return cleaned


class AddressForm(forms.ModelForm):
    """A change of address. It waits for a trustee to approve it (02 §2.3)."""

    class Meta:
        model = Address
        fields = ["line_1", "line_2", "line_3", "line_4", "postcode"]
        labels = {
            "line_1": "Address",
            "line_2": "Address line 2",
            "line_3": "Address line 3",
            "line_4": "Address line 4",
        }

    def clean_postcode(self):
        postcode = normalise_postcode(self.cleaned_data["postcode"])
        if postcode is None:
            raise forms.ValidationError("Enter a UK postcode, for example BN1 4AA.")
        return postcode

    def save_for(self, member) -> Address:
        """Replace any earlier unapproved change, so there's only ever one waiting."""
        member.addresses.filter(approved=False).delete()
        address = self.save(commit=False)
        address.member = member
        address.approved = False
        for name in ("line_1", "line_2", "line_3", "line_4"):
            setattr(address, name, getattr(address, name).strip())
        address.save()
        # TODO(notifications app): tell the trustees there's an address to approve.
        return address


class SubscriptionForm(forms.Form):
    """The monthly amount (03 §3.7). Applies from the next charge."""

    monthly_subscription = forms.IntegerField()  # replaced in __init__, so the minimum is read from Settings

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["monthly_subscription"] = subscription_field()


class LeaveForm(forms.Form):
    """Confirmation for deleting a setting-up account (03 §3.8)."""

    confirm = forms.BooleanField(label="Yes, delete my account and everything I've sent")
