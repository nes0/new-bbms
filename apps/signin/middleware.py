"""
Checks on every request for someone logged in (spec 06 §6.11):

- a member banned while logged in is logged out, with the same message the login page gives;
- the admin site needs the two-factor step for anyone who has two-factor login on, and, if the setting
  "admins must use two-factor login" is on, for every admin.
"""

from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse

from apps.site_settings.models import SiteSettings

from . import two_factor
from .forms import LoginForm
from .views import VERIFIED


class SigninMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            if user.banned:
                logout(request)
                messages.error(request, LoginForm.error_messages["banned"])
                return redirect("login")
            if request.path.startswith(reverse("admin:index")) and user.is_staff:
                response = self._admin_check(request, user)
                if response is not None:
                    return response
        return self.get_response(request)

    def _admin_check(self, request, user):
        if request.path.startswith((reverse("admin:login"), reverse("admin:logout"))):
            return None
        if two_factor.is_on(user):
            if not request.session.get(VERIFIED):
                return redirect(f"{reverse('signin:two-factor')}?next={request.get_full_path()}")
            return None
        if SiteSettings.load().admins_need_two_factor:
            messages.info(request, "The admin site needs two-factor login. Please set it up first.")
            return redirect("signin:manage")
        return None
