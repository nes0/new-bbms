from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache


def home(request):
    """The public welcome page and the signed-in member dashboard."""
    from django.urls import reverse

    from apps.access import activity, attendance
    from apps.members.models import MemberPhoto
    from apps.profiles import photos
    from apps.site_settings.models import SiteSettings

    in_space = None
    if not request.user.is_authenticated and SiteSettings.load().public_occupancy_count:
        in_space = activity.count()
    context = {"in_space": in_space}
    if request.user.is_authenticated:
        current_photo = MemberPhoto.objects.filter(member=request.user, kind=MemberPhoto.Kind.CURRENT).exists()
        visits = attendance.attendance(request.user)
        context.update(
            {
                "member_photo_url": (
                    reverse("profiles:photo", args=[request.user.pk, "thumb"])
                    if current_photo and photos.can_see(request.user, request.user)
                    else ""
                ),
                "last_visit": visits.last,
            }
        )
    return render(request, "core/home.html", context)


@never_cache
def healthz(request):
    """Used by App Platform and uptime monitoring: 200 if the app can reach its database."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:  # noqa: BLE001 - any database failure means unhealthy
        return JsonResponse({"status": "error", "database": "unreachable"}, status=503)
    return JsonResponse({"status": "ok"})


def resources(request):
    """The space's rules and policies (06 §6.8). Public, like the policies themselves."""
    from . import policies

    return render(
        request, "core/resources.html", {"core_rules": policies.CORE_RULES, "other_policies": policies.OTHER_POLICIES}
    )


def policy(request, name):
    from django.http import Http404

    from . import policies

    if name not in policies.TITLES:
        raise Http404
    body = policies.html(name, top_heading=1)
    if name == "member-agreement":  # its "rules" link points at the rules shown beside it on the induction page
        body = body.replace('href="#rules"', 'href="/resources/policy/rules/"')
    context = {"title": policies.TITLES[name], "body": body, "needs_title": not body.lstrip().startswith("<h1")}
    return render(request, "core/policy.html", context)


FEEDBACK_ROLE = "trustees"
MAX_FEEDBACK = 5000


@login_required
def feedback(request):
    """Members tell the trustees what they think of the site (06 §6.9)."""
    from django import forms
    from django.contrib import messages
    from django.shortcuts import redirect

    from apps.billing import emails
    from apps.profiles.views import is_member

    from .models import Feedback

    class FeedbackForm(forms.Form):
        comments = forms.CharField(
            label="Your thoughts", widget=forms.Textarea(attrs={"rows": 8}), max_length=MAX_FEEDBACK, strip=True
        )

    if not is_member(request.user):
        return render(request, "core/feedback.html", {"not_member": True}, status=403)
    form = FeedbackForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        item = Feedback.objects.create(member=request.user, comments=form.cleaned_data["comments"])
        emails.tell_role(FEEDBACK_ROLE, "core/emails/feedback", member=request.user, comments=item.comments)
        messages.success(request, "Thank you: your feedback has gone to the trustees.")
        return redirect("feedback")
    return render(request, "core/feedback.html", {"form": form})
