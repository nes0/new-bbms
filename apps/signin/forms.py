"""The login form: rate limited, and refuses banned members with a clear message (spec 06 §6.11)."""

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError

from . import throttle


class LoginForm(AuthenticationForm):
    remember = forms.BooleanField(required=False, initial=True, label="Keep me logged in on this device")

    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": "Your email address and password didn't match. Please try again.",
        "banned": "This account has been banned, so you can't log in. If you think that's a mistake, please contact "
        "the trustees.",
        "too_many": "Too many tries. Please wait %(minutes)s minutes and try again, or reset your password.",
    }

    def clean(self):
        email = self.cleaned_data.get("username", "")
        ip = throttle.client_ip(self.request) if self.request else ""
        if throttle.blocked("login-email", email) or throttle.blocked("login-ip", ip):
            raise ValidationError(
                self.error_messages["too_many"],
                code="too_many",
                params={"minutes": throttle.minutes("login-email")},
            )
        try:
            return super().clean()
        except ValidationError as error:
            if getattr(error, "code", None) == "invalid_login":
                throttle.record("login-email", email)
                throttle.record("login-ip", ip)
            raise

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        # Only said once the password is right, so it tells nobody else whether an account is banned.
        if user.banned:
            raise ValidationError(self.error_messages["banned"], code="banned")


class CodeForm(forms.Form):
    code = forms.CharField(
        label="Code",
        max_length=20,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric", "autofocus": True}),
    )


class PasswordCheckForm(forms.Form):
    """Asks for the member's password again before changing their two-factor login."""

    password = forms.CharField(label="Your password", strip=False, widget=forms.PasswordInput)

    def __init__(self, member, *args, **kwargs):
        self.member = member
        super().__init__(*args, **kwargs)

    def clean_password(self):
        password = self.cleaned_data["password"]
        if not self.member.check_password(password):
            raise ValidationError("That isn't your password.")
        return password
