"""Forms members fill in themselves (spec 03 §3.1)."""

import re

from django import forms
from django.contrib.auth import password_validation
from django.db import transaction
from django.utils import timezone

from . import id_documents
from .models import Address, IdCheck, IdDocument, Member

# TODO(settings app): the minimum self-service subscription is a setting (02 §2.8); legacy value £20.
MINIMUM_SUBSCRIPTION_POUNDS = 20

# The shape of a UK postcode, e.g. "BN1 4AA", "SW1A 1AA", "M1 1AE". It checks the format, not that the postcode
# exists. GIR 0AA (Girobank) is the one historical exception that is still valid.
_POSTCODE = re.compile(r"^(GIR0AA|[A-Z]{1,2}[0-9][0-9A-Z]?[0-9][A-Z]{2})$")


def normalise_postcode(value: str) -> str | None:
    """Upper case with one space before the last three characters, or None if it isn't a UK postcode."""
    compact = re.sub(r"\s+", "", value or "").upper()
    if not _POSTCODE.match(compact):
        return None
    return f"{compact[:-3]} {compact[-3:]}"


class RegistrationForm(forms.Form):
    given_name = forms.CharField(max_length=100)
    family_name = forms.CharField(max_length=100)
    email = forms.EmailField(label="Email address")
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}), help_text="At least 8 characters."
    )
    phone = forms.CharField(max_length=30, min_length=10, label="Phone number")
    address_line_1 = forms.CharField(max_length=255, label="Address")
    address_line_2 = forms.CharField(max_length=255, required=False, label="Address line 2")
    address_line_3 = forms.CharField(max_length=255, required=False, label="Address line 3")
    address_line_4 = forms.CharField(max_length=255, required=False, label="Address line 4")
    postcode = forms.CharField(max_length=10)
    monthly_subscription = forms.IntegerField(
        label="Monthly subscription (£)",
        min_value=MINIMUM_SUBSCRIPTION_POUNDS,
        max_value=10_000,
        help_text=f"Whole pounds, at least £{MINIMUM_SUBSCRIPTION_POUNDS}.",
    )
    emergency_contact = forms.CharField(
        max_length=255, help_text="Who we should contact in an emergency, and how (name and phone number)."
    )
    # TODO(photos): an optional profile photo, into the pending-approval queue (03 §3.1, 06 §6.1). Waits for
    # private file storage (DigitalOcean Spaces, 02 §2.9) and the photo approval workflow.
    rules_agreed = forms.BooleanField(label="I agree to follow the rules of the space")
    visited_space = forms.BooleanField(label="I have visited the space")

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if Member.objects.filter(email=email).exists():
            raise forms.ValidationError("There is already an account with that email address. Try logging in.")
        return email

    def clean_postcode(self):
        postcode = normalise_postcode(self.cleaned_data["postcode"])
        if postcode is None:
            raise forms.ValidationError("Enter a UK postcode, for example BN1 4AA.")
        return postcode

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        if password:
            # Checked against the details given, so a password can't just be the member's name or email.
            candidate = Member(
                email=cleaned.get("email", ""),
                given_name=cleaned.get("given_name", ""),
                family_name=cleaned.get("family_name", ""),
            )
            try:
                password_validation.validate_password(password, candidate)
            except forms.ValidationError as error:
                self.add_error("password", error)
        return cleaned

    @transaction.atomic
    def save(self) -> Member:
        data = self.cleaned_data
        member = Member.objects.create_user(
            data["email"],
            data["password"],
            given_name=data["given_name"].strip(),
            family_name=data["family_name"].strip(),
            phone=data["phone"].strip(),
            emergency_contact=data["emergency_contact"].strip(),
            monthly_subscription_pence=data["monthly_subscription"] * 100,
            rules_agreed_at=timezone.now(),
            status=Member.Status.SETTING_UP,
        )
        # A member's own address waits for an admin to approve it (02 §2.3).
        Address.objects.create(
            member=member,
            line_1=data["address_line_1"].strip(),
            line_2=data["address_line_2"].strip(),
            line_3=data["address_line_3"].strip(),
            line_4=data["address_line_4"].strip(),
            postcode=data["postcode"],
            approved=False,
        )
        return member


class IdDocumentsForm(forms.Form):
    """Proof of identity: photo ID and proof of address (03 §3.1). Either can be sent on its own, or replaced."""

    ACCEPT = ".pdf,.jpg,.jpeg,.png,.webp,.heic,.heif,application/pdf,image/*"

    photo_id = forms.FileField(
        required=False,
        label=IdDocument.Kind.PHOTO_ID.label,
        widget=forms.ClearableFileInput(attrs={"accept": ACCEPT}),
    )
    proof_of_address = forms.FileField(
        required=False,
        label=IdDocument.Kind.PROOF_OF_ADDRESS.label,
        help_text="Dated in the last three months, showing your name and the address you joined with.",
        widget=forms.ClearableFileInput(attrs={"accept": ACCEPT}),
    )

    def _clean_document(self, name):
        upload = self.cleaned_data.get(name)
        if not upload:
            return None
        if upload.size > id_documents.MAX_FILE_BYTES:
            raise forms.ValidationError("That file is too big: the limit is 10 MB.")
        data = upload.read()
        content_type = id_documents.sniff_content_type(data)
        if content_type is None:
            raise forms.ValidationError("Please send a PDF or a photo (JPEG, PNG, WebP or HEIC).")
        return upload, data, content_type

    def clean_photo_id(self):
        return self._clean_document("photo_id")

    def clean_proof_of_address(self):
        return self._clean_document("proof_of_address")

    def clean(self):
        cleaned = super().clean()
        if not self.errors and not cleaned.get("photo_id") and not cleaned.get("proof_of_address"):
            raise forms.ValidationError("Choose a file to send.")
        return cleaned

    def save(self, member):
        for kind in IdDocument.Kind.values:
            document = self.cleaned_data.get(kind)
            if document:
                id_documents.save_document(member, kind, *document)


class IdCheckForm(forms.Form):
    result = forms.ChoiceField(choices=IdCheck.Result.choices, widget=forms.RadioSelect)
    note = forms.CharField(
        max_length=255,
        required=False,
        help_text='What you saw, e.g. "passport, council tax bill". If you didn\'t accept them, say why: '
        "the member is asked to send them again.",
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("result") == IdCheck.Result.REJECTED and not cleaned.get("note"):
            self.add_error("note", "Say why, so the member knows what to send instead.")
        return cleaned
