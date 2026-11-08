"""
Proposals and votes (spec 06 §6.3). The rules (who may vote, opening, counting) are in `voting.py`.

A proposal is open for voting from the start of its start date until the end of its end date (UK time). The ballot
is secret: members see only their own vote and, once voting closes, the totals.
"""

from datetime import datetime, time, timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


class Proposal(models.Model):
    title = models.CharField(max_length=100)
    description = models.TextField(
        max_length=20_000, help_text="Markdown: **bold**, *italic*, [a link](https://…), lists with - or 1."
    )
    start_date = models.DateField(help_text="Voting opens at the start of this day.")
    end_date = models.DateField(help_text="Voting closes at the end of this day.")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    # Fixed when voting opens (by the `update_proposals` job), so the quorum can't move during the vote.
    opened_at = models.DateTimeField(null=True, blank=True)
    eligible_voters = models.PositiveIntegerField(null=True, blank=True, help_text="Members who could vote.")
    quorum_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    opening_posted = models.BooleanField(default=False, help_text="Announced on Discord.")

    # The result, counted once voting has closed.
    counted_at = models.DateTimeField(null=True, blank=True)
    votes_for = models.PositiveIntegerField(default=0)
    votes_neutral = models.PositiveIntegerField(default=0)
    votes_against = models.PositiveIntegerField(default=0)
    abstentions = models.PositiveIntegerField(default=0)
    quorum_met = models.BooleanField(null=True)
    passed = models.BooleanField(null=True)
    result_posted = models.BooleanField(default=False, help_text="Result announced on Discord.")

    legacy_id = models.PositiveIntegerField(null=True, blank=True, unique=True)

    class Meta:
        ordering = ["-end_date", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_date__gte=models.F("start_date")), name="proposal_ends_after_it_starts"
            )
        ]

    def __str__(self):
        return self.title

    # -- When voting happens ---------------------------------------------------------------------------------------

    @property
    def opens_at(self) -> datetime:
        return timezone.make_aware(datetime.combine(self.start_date, time.min))

    @property
    def closes_at(self) -> datetime:
        return timezone.make_aware(datetime.combine(self.end_date + timedelta(days=1), time.min))

    def has_started(self, now=None) -> bool:
        return (now or timezone.now()) >= self.opens_at

    def has_closed(self, now=None) -> bool:
        return (now or timezone.now()) >= self.closes_at

    def is_open(self, now=None) -> bool:
        return self.has_started(now) and not self.has_closed(now)

    def status(self, now=None) -> str:
        if not self.has_started(now):
            return "upcoming"
        return "closed" if self.has_closed(now) else "open"

    # -- The result ------------------------------------------------------------------------------------------------

    @property
    def votes_cast(self) -> int:
        """For, neutral and against (legacy's `votes_cast`): not abstentions."""
        return self.votes_for + self.votes_neutral + self.votes_against

    @property
    def turnout(self) -> int:
        """Everyone who voted, abstentions included: what the quorum counts."""
        return self.votes_cast + self.abstentions

    @property
    def result(self) -> int:
        return self.votes_for - self.votes_against

    @property
    def quorum_needed(self) -> int:
        """The smallest turnout that meets the quorum (0 when there's no quorum)."""
        if not self.quorum_percent or not self.eligible_voters:
            return 0
        return -(-self.eligible_voters * self.quorum_percent // 100)  # rounded up

    def outcome(self) -> str:
        """In words, once counted, e.g. "Passed: 12 for, 3 against"."""
        if self.counted_at is None:
            return ""
        words = "Passed" if self.passed else "Not passed"
        detail = f"{self.votes_for} for, {self.votes_against} against, {self.votes_neutral} neutral"
        if self.abstentions:
            detail += f", {self.abstentions} abstained"
        if self.quorum_met is False:
            detail += f" (quorum not met: {self.turnout} of the {self.quorum_needed} needed voted)"
        return f"{words}: {detail}"


class Vote(models.Model):
    """One member's vote on a proposal. They can change it while voting is open."""

    class Choice(models.IntegerChoices):
        FOR = 1, "For"
        NEUTRAL = 0, "Neutral"
        AGAINST = -1, "Against"

    proposal = models.ForeignKey(Proposal, on_delete=models.CASCADE, related_name="votes")
    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="proposal_votes")
    choice = models.SmallIntegerField(choices=Choice.choices, null=True, blank=True, help_text="Empty: abstained.")
    abstain = models.BooleanField(default=False)
    voted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["proposal", "member"], name="one_vote_per_member_per_proposal"),
            models.CheckConstraint(
                condition=models.Q(abstain=True, choice__isnull=True) | models.Q(abstain=False, choice__isnull=False),
                name="a_vote_is_a_choice_or_an_abstention",
            ),
        ]

    def __str__(self):
        return f"Vote on {self.proposal}"

    @property
    def label(self) -> str:
        return "Abstain" if self.abstain else self.Choice(self.choice).label
