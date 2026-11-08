"""The activity page and who's in the space (spec 06 §6.7). Members see names; visitors at most a count."""

from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache

from apps.members.models import Member
from apps.profiles.views import LISTED_STATUSES, _card, _with_photos, is_member
from apps.site_settings.models import SiteSettings

from . import activity


def _members_only(request):
    if not is_member(request.user):
        raise Http404


def _cards(viewer, members) -> dict:
    """{member id: card} for showing each member with their photo (or initials)."""
    cards = {}
    for member in _with_photos(Member.objects.filter(pk__in={m.pk for m in members})):
        card = _card(viewer, member)
        if member.status not in LISTED_STATUSES:
            card["photo_url"] = ""  # their photo is only served while they're listed in the directory
        card["listed"] = member.status in LISTED_STATUSES and not member.banned
        cards[member.pk] = card
    return cards


def _in_now_context(request):
    people = activity.in_space()
    cards = _cards(request.user, [p.member for p in people])
    return {
        "people": [{"presence": p, "card": cards[p.member.pk]} for p in people],
        "updated": timezone.now(),
    }


@login_required
def activity_page(request):
    _members_only(request)
    today = timezone.localdate()
    try:
        chosen = date.fromisoformat(request.GET.get("date", "")) if request.GET.get("date") else today
    except ValueError:
        chosen = today
    chosen = min(chosen, today)
    moments = activity.day(chosen)
    cards = _cards(request.user, [m.member for m in moments])
    context = {
        "date": chosen,
        "today": today,
        "previous": chosen - timedelta(days=1),
        "next": chosen + timedelta(days=1) if chosen < today else None,
        "moments": [{"moment": m, "card": cards[m.member.pk]} for m in moments],
        "visitors": len(cards),
        **_in_now_context(request),
    }
    return render(request, "access/activity.html", context)


@never_cache
@login_required
def in_now(request):
    """The "in the space now" panel, which the activity page refreshes every minute."""
    _members_only(request)
    return render(request, "access/_in_now.html", _in_now_context(request))


@never_cache
def occupancy(request):
    """How many people are in the space, for the space's website, if the trustees turned it on. Never names."""
    if not SiteSettings.load().public_occupancy_count:
        raise Http404
    response = JsonResponse({"in_space": activity.count(), "as_of": timezone.now().isoformat(timespec="seconds")})
    response["Access-Control-Allow-Origin"] = "*"
    return response
