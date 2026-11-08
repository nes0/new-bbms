"""The member's profile form (spec 06 §6.1)."""

import re

from django import forms
from django.db import models

from . import skills
from .models import Profile

USERNAME = re.compile(r"^[A-Za-z0-9_.\-]{1,50}$")
# What people paste instead of a username: their profile's address, or "@name".
PREFIXES = re.compile(r"^(https?://)?(www\.)?(github\.com|twitter\.com|x\.com|facebook\.com|fb\.com)/|^@", re.I)


def _formfield(field, **kwargs):
    if isinstance(field, models.URLField):
        kwargs["assume_scheme"] = "https"  # "example.com" means https://example.com
    return field.formfield(**kwargs)


class ProfileForm(forms.ModelForm):
    skills = forms.MultipleChoiceField(
        choices=skills.CHOICES, required=False, widget=forms.CheckboxSelectMultiple, label="Skills"
    )
    photo = forms.FileField(
        required=False,
        label="New photo",
        help_text="A clear photo of your face: it's how people at the space know who you are, and key holders need "
        "one for the door. It's cropped to a square from the middle, and a trustee checks it before it shows.",
        widget=forms.ClearableFileInput(attrs={"accept": "image/jpeg,image/png,image/webp"}),
    )

    private = forms.BooleanField(
        required=False,
        label="Keep my profile private",
        help_text="Only logged-in members see it; it's never shown to visitors.",
    )

    class Meta:
        model = Profile
        fields = [
            "tagline",
            "description",
            "skills",
            "website",
            "github",
            "twitter",
            "facebook",
            "irc",
            "photo_private",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 6})}
        help_texts = {"website": "Your site's address, e.g. https://example.com"}
        formfield_callback = _formfield

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["private"].initial = self.instance.member.profile_private

    def save(self, commit=True):
        profile = super().save(commit)
        member = profile.member
        if member.profile_private != self.cleaned_data["private"]:
            member.profile_private = self.cleaned_data["private"]
            member.save(update_fields=["profile_private"])
        return profile

    def _username(self, name):
        value = PREFIXES.sub("", self.cleaned_data[name].strip()).strip("/")
        if value and not USERNAME.match(value):
            raise forms.ValidationError("Just the username, please: letters, numbers, dots, dashes and underscores.")
        return value

    def clean_github(self):
        return self._username("github")

    def clean_twitter(self):
        return self._username("twitter")

    def clean_facebook(self):
        return self._username("facebook")

    def clean_irc(self):
        return self.cleaned_data["irc"].strip()

    def clean_website(self):
        url = self.cleaned_data["website"].strip()
        if url and not url.lower().startswith(("http://", "https://")):
            raise forms.ValidationError("Enter a web address starting https://")
        return url

    def clean_tagline(self):
        return self.cleaned_data["tagline"].strip()

    def clean_description(self):
        return self.cleaned_data["description"].strip()
