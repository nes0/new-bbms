"""
The vouching page (spec 05 §5.10): active members vouch for, or discreetly raise a concern about, members waiting for
trusted status. A voucher sees only their own choices: no counts and no other names.
"""

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.access import attendance
from apps.profiles.views import _card, _with_photos, is_member

from . import vouching
from .models import VouchChange


def _members_only(request):
    if not is_member(request.user):
        raise Http404


def _row(request, member, vouch, first_visit=None, **extra):
    return {
        "card": _card(request.user, member),
        "vouch": vouch,
        "first_visit": first_visit,
        "note_length": vouching.NOTE_LENGTH,
        **extra,
    }


@login_required
def index(request):
    _members_only(request)
    why_not = vouching.why_cant_vouch(request.user)
    rows = []
    if not why_not:
        mine = vouching.my_choices(request.user)
        members = list(_with_photos(vouching.waiting_members(request.user)))
        days = attendance.visit_days([member.pk for member in members])
        rows = [_row(request, m, mine.get(m.pk), min(days.get(m.pk, ()), default=None)) for m in members]
    return render(request, "vouching/index.html", {"why_not": why_not, "rows": rows})


@login_required
@require_POST
def choose(request, member_id):
    _members_only(request)
    member = get_object_or_404(get_user_model(), pk=member_id)
    action = request.POST.get("action", "")
    error, vouch = "", None
    try:
        vouch = vouching.record(member, request.user, action, request.POST.get("note", ""))
    except vouching.CantVouch as problem:
        error = str(problem)
        vouch = vouching.my_choices(request.user).get(member.pk)
    if request.headers.get("HX-Request"):  # the member's row swaps itself in place
        member = _with_photos(get_user_model().objects.filter(pk=member.pk)).get()
        first = min(attendance.visit_days([member.pk]).get(member.pk, ()), default=None)
        context = _row(request, member, vouch, first, error=error, saved=not error)
        return render(request, "vouching/_row.html", context)
    if error:
        messages.error(request, error)
    else:
        done = {
            VouchChange.Action.VOUCH: f"You vouched for {member.full_name}.",
            VouchChange.Action.CONCERN: f"Your concern about {member.full_name} is with the trustees only.",
            VouchChange.Action.WITHDRAW: f"Withdrawn: nothing from you counts for {member.full_name} now.",
        }
        messages.success(request, done[action])
    return redirect(f"{reverse('vouching:index')}#member-{member.pk}")
