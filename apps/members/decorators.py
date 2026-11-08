"""Views that need a confirmed email address (spec 03 §3.1)."""

from functools import wraps

from django.shortcuts import render


def email_confirmed_required(view):
    """Put under `@login_required`. A member who hasn't confirmed their email address (or whose confirmation an admin
    has withdrawn) sees a page asking them to, instead of the view. Used for everything a member starts that moves
    money. Nightly billing and webhooks aren't views a member starts, so they carry on."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.email_verified:
            return render(request, "members/confirm_email_first.html", {"what": "pay"}, status=403)
        return view(request, *args, **kwargs)

    return wrapper
