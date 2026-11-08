"""Forms on the member's own account page (spec 03 §3.7–3.8, 02 §2.3)."""

from django import forms
from django.core.validators import MinLengthValidator
from django.db.models import Q

from . import email_confirmation, notices
from .forms import normalise_postcode, subscription_field
from .models import Address, Member


class DetailsForm(forms.ModelForm):
    """Name, phone, emergency contact and PayPal email. The email address is the login, so it isn't changed here."""

    class Meta:
        model = Member
        fields = ["given_name", "family_name", "phone", "emergency_contact", "paypal_email"]
        labels = {"phone": "Phone number"}
        help_texts = {
            "emergency_contact": "Who we should contact in an emergency, and how (name and phone number).",
            "paypal_email": "Only if you pay by PayPal from a different address, so we can match your payments.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Required for members, as at registration (02 §2.2); only admins may leave them empty.
        for name in ("given_name", "family_name", "phone", "emergency_contact"):
            self.fields[name].required = True
        self.fields["phone"].validators.append(MinLengthValidator(10))

    def clean_paypal_email(self):
        email = self.cleaned_data["paypal_email"].strip().lower()
        if email == self.instance.email.lower():
            return ""  # their own address is matched anyway
        others = Member.objects.exclude(pk=self.instance.pk)
        if email and others.filter(Q(email__iexact=email) | Q(paypal_email__iexact=email)).exists():
            raise forms.ValidationError("That address belongs to another account. Please ask a trustee.")
        return email

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
        notices.address_waiting(address)
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


class LeaveMembershipForm(forms.Form):
    """Confirmation for leaving (03 §3.8): the Direct Debit stops, access lasts until the paid-up date."""

    confirm = forms.BooleanField(label="Yes, I want to leave and stop my monthly payments")


class InductionForm(forms.Form):
    """The general induction (05 §5.9): both must be ticked, as in legacy."""

    induction_completed = forms.BooleanField(
        label="I have read and understand this guidance for members and I agree to follow it."
    )
    rules_agreed = forms.BooleanField(label="I agree to the rules of the space.")


class EmailChangeForm(forms.Form):
    """Log in with a different email address (03 §3.1). The password is asked for again, so someone at an unlocked
    laptop can't redirect the account's email."""

    new_email = forms.EmailField(label="New email address")
    password = forms.CharField(
        label="Your password", widget=forms.PasswordInput(attrs={"autocomplete": "current-password"})
    )

    def __init__(self, member, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.member = member

    def clean_new_email(self):
        email = self.cleaned_data["new_email"].strip().lower()
        if email == self.member.email.lower():
            raise forms.ValidationError("That is the address you already have.")
        if email_confirmation.address_in_use(email, except_member=self.member):
            raise forms.ValidationError("There is already an account with that email address.")
        return email

    def clean_password(self):
        if not self.member.check_password(self.cleaned_data["password"]):
            raise forms.ValidationError("That isn't your password.")
        return self.cleaned_data["password"]
