"""
Who may vote, casting a vote, opening a proposal and counting the result (spec 06 §6.3).

- **Voters** are current members: active, leaving or honorary, not banned, and not storage-only accounts (the
  `storage-box-user` role). Suspended, left and joining accounts can't vote (decided 2026-11-08).
- **Opening**: when voting opens, the `update_proposals` job fixes the number of members who can vote and the quorum
  (Settings → Proposals), emails every voter and posts on Discord.
- **Counting**, once voting has closed: for, neutral, against and abstentions. The **quorum** is a share of the
  members who could vote when it opened; abstentions count towards it. A proposal **passes** if the quorum is met and
  more members voted for than against.
"""

import logging

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from apps.billing import emails
from apps.discord import client as discord
from apps.members.models import Member
from apps.site_settings.models import SiteSettings

from .models import Proposal, Vote

logger = logging.getLogger(__name__)

VOTER_STATUSES = (Member.Status.ACTIVE, Member.Status.LEAVING, Member.Status.HONORARY)
STORAGE_ONLY_ROLE = "storage-box-user"


class CantVote(Exception):
    """Why a vote wasn't recorded. The message is shown to the member."""


def voters():
    """Every member who may vote."""
    return (
        Member.objects.filter(status__in=VOTER_STATUSES, banned=False, is_active=True)
        .exclude(groups__name=STORAGE_ONLY_ROLE)
        .distinct()
    )


def why_cant_vote(member) -> str:
    """ "" if the member may vote, otherwise the reason, for them to read."""
    if not member.is_authenticated:
        return "Please log in to vote."
    if member.banned or member.status not in VOTER_STATUSES or not member.is_active:
        return "Only current members can vote."
    if member.groups.filter(name=STORAGE_ONLY_ROLE).exists():
        return "Storage-only accounts can't vote."
    return ""


def cast(proposal, member, choice: str, now=None) -> Vote:
    """Record (or change) a member's vote. `choice` is "1", "0", "-1" or "abstain". Raises CantVote."""
    if reason := why_cant_vote(member):
        raise CantVote(reason)
    if not proposal.is_open(now):
        raise CantVote("Voting on this proposal isn't open.")
    if choice == "abstain":
        values = {"choice": None, "abstain": True}
    elif choice in ("1", "0", "-1", "+1"):
        values = {"choice": int(choice), "abstain": False}
    else:
        raise CantVote("Please choose For, Neutral, Against or Abstain.")
    vote, _ = Vote.objects.update_or_create(
        proposal=proposal, member=member, defaults={**values, "voted_at": now or timezone.now()}
    )
    return vote


def proposals_waiting_for(member, now=None) -> list:
    """Open proposals the member may vote on and hasn't yet (for their account page)."""
    if why_cant_vote(member):
        return []
    today = timezone.localdate(now)
    waiting = Proposal.objects.filter(start_date__lte=today, end_date__gte=today).exclude(votes__member=member)
    return list(waiting.order_by("end_date"))


def turnout(proposal) -> int:
    return proposal.votes.count()


def link(proposal) -> str:
    return f"{settings.SITE_URL}{reverse('proposals:detail', args=[proposal.pk])}"


# -- Opening ------------------------------------------------------------------------------------------------------


def open_voting(proposal_id, now=None) -> bool:
    """Fix the voters and the quorum, then tell members. Does nothing if already done. Returns True if it opened it."""
    now = now or timezone.now()
    with transaction.atomic():
        proposal = Proposal.objects.select_for_update().get(pk=proposal_id)
        if proposal.opened_at or not proposal.has_started(now):
            return False
        everyone = list(voters())
        proposal.opened_at = now
        proposal.eligible_voters = len(everyone)
        proposal.quorum_percent = SiteSettings.load().proposal_quorum_percent
        proposal.save(update_fields=["opened_at", "eligible_voters", "quorum_percent"])
    # Told after the change is saved, so a second run never emails anyone twice.
    if not proposal.has_closed(now):
        for member in everyone:
            try:
                emails.send(member, "proposals/emails/open", proposal=proposal, link=link(proposal))
            except Exception:  # one bad address mustn't stop the rest
                logger.exception("Proposal %s: couldn't email member %s", proposal.pk, member.pk)
    logger.info("Proposal %s: voting opened, %s members can vote", proposal.pk, proposal.eligible_voters)
    return True


# -- Counting -----------------------------------------------------------------------------------------------------


def count(proposal_id, now=None) -> bool:
    """Count the votes of a proposal whose voting has closed. Does nothing if already counted. Returns True if it
    counted it.
    """
    now = now or timezone.now()
    with transaction.atomic():
        proposal = Proposal.objects.select_for_update().get(pk=proposal_id)
        if proposal.counted_at or not proposal.has_closed(now):
            return False
        if proposal.eligible_voters is None:  # it was never opened (the job didn't run while it was open)
            proposal.eligible_voters = voters().count()
            proposal.quorum_percent = SiteSettings.load().proposal_quorum_percent
        totals = proposal.votes.aggregate(
            votes_for=Count("pk", filter=Q(abstain=False, choice=Vote.Choice.FOR)),
            votes_neutral=Count("pk", filter=Q(abstain=False, choice=Vote.Choice.NEUTRAL)),
            votes_against=Count("pk", filter=Q(abstain=False, choice=Vote.Choice.AGAINST)),
            abstentions=Count("pk", filter=Q(abstain=True)),
        )
        for name, value in totals.items():
            setattr(proposal, name, value)
        proposal.quorum_met = proposal.turnout >= proposal.quorum_needed
        proposal.passed = proposal.quorum_met and proposal.votes_for > proposal.votes_against
        proposal.counted_at = now
        proposal.save()
    logger.info("Proposal %s counted: %s", proposal.pk, proposal.outcome())
    return True


# -- Discord ------------------------------------------------------------------------------------------------------


def _post(text) -> bool:
    channel = discord.announcement_channel()
    if not discord.enabled() or not channel:
        return True  # nowhere to post: nothing to retry
    try:
        discord.client().post(channel, text[:2000])
    except discord.DiscordError as error:
        logger.warning("Proposal announcement not posted yet: %s", error)
        return False
    return True


def post_announcements(now=None):
    """Post on Discord when voting opens and when the result is in; tried again next time if Discord was down."""
    now = now or timezone.now()
    for proposal in Proposal.objects.filter(opening_posted=False, opened_at__isnull=False):
        if proposal.has_closed(now):
            ok = True  # too late to invite votes: the result post covers it
        else:
            end = f"{proposal.end_date:%A} {proposal.end_date.day} {proposal.end_date:%B}"
            ok = _post(
                f"**New proposal: {proposal.title}**\n"
                f"Voting is open until the end of {end}. Read it and vote: {link(proposal)}"
            )
        if ok:
            Proposal.objects.filter(pk=proposal.pk).update(opening_posted=True)
    for proposal in Proposal.objects.filter(result_posted=False, counted_at__isnull=False):
        if _post(f"**Proposal result: {proposal.title}**\n{proposal.outcome()}.\n{link(proposal)}"):
            Proposal.objects.filter(pk=proposal.pk).update(result_posted=True)


def update_all(now=None) -> dict:
    """The hourly job: open what has started, count what has closed, announce both."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    opened = [
        pk
        for pk in Proposal.objects.filter(opened_at__isnull=True, start_date__lte=today).values_list("pk", flat=True)
        if open_voting(pk, now)
    ]
    counted = [
        pk
        for pk in Proposal.objects.filter(counted_at__isnull=True, end_date__lt=today).values_list("pk", flat=True)
        if count(pk, now)
    ]
    if not emails.is_suppressed():
        post_announcements(now)
    return {"opened": len(opened), "counted": len(counted)}
