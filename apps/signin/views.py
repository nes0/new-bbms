"""
Logging in (spec 06 §6.11): the login page, the two-factor step, password resets with a limit, and the member's
two-factor settings page.

With two-factor login on, a correct password doesn't log the member in. It's remembered in the session for a few
minutes ("pending"), and they're logged in once they give a code.
"""

import time

from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST

from apps.access import qr
from apps.site_settings.models import SiteSettings

from . import throttle, totp, two_factor
from .forms import CodeForm, LoginForm, PasswordCheckForm
from .models import TwoFactor

PENDING = "signin.pending"
VERIFIED = "signin.verified"  # this session has passed the two-factor step
PENDING_SECONDS = 300


def _safe(request, url, default="home"):
    if url and url_has_allowed_host_and_scheme(url, {request.get_host()}, request.is_secure()):
        return url
    return reverse(default)


def _remember(request, remember: bool):
    if not remember:
        request.session.set_expiry(0)  # ends when the browser closes


class LoginView(auth_views.LoginView):
    form_class = LoginForm
    redirect_authenticated_user = True

    def form_valid(self, form):
        member = form.get_user()
        throttle.clear("login-email", form.cleaned_data.get("username", ""))
        remember = form.cleaned_data.get("remember", False)
        if two_factor.is_on(member):
            self.request.session[PENDING] = {
                "member": member.pk,
                "backend": member.backend,
                "until": time.time() + PENDING_SECONDS,
                "remember": remember,
                "next": self.get_success_url(),
            }
            return redirect("signin:two-factor")
        login(self.request, member)
        _remember(self.request, remember)
        return redirect(self.get_success_url())


@require_http_methods(["GET", "POST"])
def two_factor_step(request):
    """Ask for the code: after the password at login, or before the admin site for a session that hasn't given one."""
    pending = request.session.get(PENDING)
    if pending and pending["until"] < time.time():
        request.session.pop(PENDING, None)
        messages.info(request, "That took too long. Please log in again.")
        return redirect("login")
    if pending:
        member = get_user_model().objects.filter(pk=pending["member"]).first()
        target = pending["next"]
    elif request.user.is_authenticated and two_factor.is_on(request.user):
        member, target = request.user, _safe(request, request.GET.get("next") or request.POST.get("next"))
    else:
        return redirect("login")
    if member is None:
        return redirect("login")

    form = CodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if throttle.blocked("two-factor", member.pk):
            request.session.pop(PENDING, None)
            messages.error(
                request, f"Too many wrong codes. Please wait {throttle.minutes('two-factor')} minutes and try again."
            )
            return redirect("login")
        used = two_factor.verify(member, form.cleaned_data["code"])
        if used:
            throttle.clear("two-factor", member.pk)
            if pending:
                request.session.pop(PENDING, None)
                member.backend = pending["backend"]
                login(request, member)
                _remember(request, pending["remember"])
            request.session[VERIFIED] = True
            if used == "recovery":
                left = two_factor.recovery_codes_left(member)
                messages.warning(
                    request,
                    f"You used a recovery code; {left} left. If you've lost your phone, set up two-factor login "
                    "again from your account page.",
                )
            return redirect(_safe(request, target))
        throttle.record("two-factor", member.pk)
        form.add_error("code", "That code isn't right. Check the time on your phone, then try the newest code.")
    return render(request, "signin/two_factor.html", {"form": form, "next": target, "logging_in": bool(pending)})


class PasswordResetView(auth_views.PasswordResetView):
    """Django's reset page, with a limit on how many emails one address (or one visitor) can ask for."""

    def form_valid(self, form):
        email = form.cleaned_data["email"]
        ip = throttle.client_ip(self.request)
        if throttle.blocked("reset-email", email) or throttle.blocked("reset-ip", ip):
            form.add_error(
                "email",
                f"We've sent several reset emails already. Please check your inbox (and spam folder), or try again "
                f"in {throttle.minutes('reset-email')} minutes.",
            )
            return self.form_invalid(form)
        throttle.record("reset-email", email)
        throttle.record("reset-ip", ip)
        return super().form_valid(form)


def admin_login(request):
    """The admin site's own login page is skipped, so every login is limited and checked the same way."""
    return redirect(f"{reverse('login')}?next={_safe(request, request.GET.get('next'), 'admin:index')}")


# --- the member's two-factor settings (/account/two-factor/) ---


def _qr_svg(uri: str) -> str:
    return qr.make(uri).svg_inline(scale=5, dark="#000", light="#fff")


@login_required
@require_http_methods(["GET", "POST"])
def manage(request):
    member = request.user
    if two_factor.is_on(member):
        return render(
            request,
            "signin/manage.html",
            {"left": two_factor.recovery_codes_left(member), "password_form": PasswordCheckForm(member)},
        )
    form = CodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        codes = two_factor.confirm(member, form.cleaned_data["code"])
        if codes is not None:
            request.session[VERIFIED] = True
            return render(request, "signin/recovery_codes.html", {"codes": codes, "new": True})
        form.add_error("code", "That code isn't right. Check the time on your phone, then try the newest code.")
        device = TwoFactor.objects.get(member=member)
    else:
        device = two_factor.start(member)
    uri = totp.setup_uri(device.secret, member.email, SiteSettings.load().space_name)
    context = {"form": form, "secret": device.secret, "qr": _qr_svg(uri)}
    return render(request, "signin/setup.html", context)


@login_required
@require_POST
def new_codes(request):
    form = PasswordCheckForm(request.user, request.POST)
    if not two_factor.is_on(request.user) or not form.is_valid():
        messages.error(request, "That isn't your password.")
        return redirect("signin:manage")
    return render(request, "signin/recovery_codes.html", {"codes": two_factor.new_recovery_codes(request.user)})


@login_required
@require_POST
def turn_off(request):
    form = PasswordCheckForm(request.user, request.POST)
    if not form.is_valid():
        messages.error(request, "That isn't your password.")
        return redirect("signin:manage")
    two_factor.turn_off(request.user)
    request.session.pop(VERIFIED, None)
    messages.success(request, "Two-factor login is off. You'll log in with just your password.")
    return redirect("signin:manage")
