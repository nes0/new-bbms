"""
The proposals pages (spec 06 §6.3). Members (anyone who sees the member directory) read proposals; current members
vote. The ballot is secret: a member sees their own vote, the number of members who have voted so far, and the
totals once voting has closed. Trustees write proposals in the admin.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.markup import to_html
from apps.profiles.views import is_member
from apps.site_settings.models import SiteSettings

from . import voting
from .models import Proposal, Vote


def _members_only(request):
    if not is_member(request.user):
        raise Http404


@login_required
def index(request):
    _members_only(request)
    proposals = list(Proposal.objects.all())
    voted = set(request.user.proposal_votes.values_list("proposal_id", flat=True))
    sections = {"open": [], "upcoming": [], "closed": []}
    for proposal in proposals:
        proposal.has_voted = proposal.pk in voted
        sections[proposal.status()].append(proposal)
    context = {
        "sections": sections,
        "can_add": request.user.has_perm("proposals.add_proposal"),
        "can_vote": not voting.why_cant_vote(request.user),
        "quorum": SiteSettings.load().proposal_quorum_percent,
    }
    return render(request, "proposals/index.html", context)


def _vote_context(request, proposal, vote=None):
    if vote is None:
        vote = Vote.objects.filter(proposal=proposal, member=request.user).first()
    return {
        "proposal": proposal,
        "vote": vote,
        "current": "abstain" if vote and vote.abstain else (str(vote.choice) if vote else ""),
        "why_not": voting.why_cant_vote(request.user),
        "turnout": voting.turnout(proposal),
        "choices": [
            ("1", "For", "I'm in favour of this proposal"),
            ("0", "Neutral", "I have no strong opinion"),
            ("-1", "Against", "I'm not in favour of this proposal"),
            ("abstain", "Abstain", "I'm choosing not to vote either way (counts towards the quorum)"),
        ],
    }


@login_required
def detail(request, proposal_id):
    _members_only(request)
    proposal = get_object_or_404(Proposal, pk=proposal_id)
    context = {
        **_vote_context(request, proposal),
        "description": to_html(proposal.description),
        "can_change": request.user.has_perm("proposals.change_proposal"),
    }
    return render(request, "proposals/detail.html", context)


@login_required
@require_POST
def vote(request, proposal_id):
    _members_only(request)
    proposal = get_object_or_404(Proposal, pk=proposal_id)
    error, cast = "", None
    try:
        cast = voting.cast(proposal, request.user, request.POST.get("vote", ""))
    except voting.CantVote as problem:
        error = str(problem)
    if request.headers.get("HX-Request"):  # the vote form swaps itself in place
        context = {**_vote_context(request, proposal, cast), "error": error, "saved": cast is not None}
        return render(request, "proposals/_vote.html", context)
    if error:
        messages.error(request, error)
    else:
        messages.success(request, f"Your vote ({cast.label}) is recorded. You can change it until voting closes.")
    return redirect("proposals:detail", proposal.pk)
