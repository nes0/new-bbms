"""Proposals and voting (spec 06 §6.3)."""

from datetime import date, datetime, timedelta
from io import StringIO

from django.contrib.auth.models import Group
from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.discord import client as discord
from apps.equipment.tests.helpers import member as make_member
from apps.members.models import Member
from apps.proposals import voting
from apps.proposals.models import Proposal, Vote
from apps.site_settings.models import SiteSettings

TODAY = timezone.localdate()


def proposal(start=0, end=3, **fields):
    """A proposal whose voting starts `start` days from today and ends `end` days from today."""
    defaults = dict(title="Buy a new laser", description="We **should**.")
    defaults.update(fields)
    return Proposal.objects.create(
        start_date=TODAY + timedelta(days=start), end_date=TODAY + timedelta(days=end), **defaults
    )


def at(day: date, hour=12):
    return timezone.make_aware(datetime(day.year, day.month, day.day, hour))


def set_quorum(percent):
    config = SiteSettings.load()
    config.proposal_quorum_percent = percent
    config.save()


class ProposalTestCase(TestCase):
    def setUp(self):
        self.discord = discord.reset_fake()
        self.kim = make_member(given_name="Kim", family_name="Lee")
        self.sam = make_member(given_name="Sam", family_name="Ray")


class WhoCanVoteTests(ProposalTestCase):
    def test_current_members_only(self):
        leaving = make_member(status=Member.Status.LEAVING)
        honorary = make_member(status=Member.Status.HONORARY)
        cannot = [
            make_member(status=Member.Status.SUSPENDED),
            make_member(status=Member.Status.LEFT),
            make_member(status=Member.Status.SETTING_UP),
            make_member(banned=True),
        ]
        storage_only = make_member()
        Group.objects.create(name="storage-box-user").user_set.add(storage_only)
        cannot.append(storage_only)
        self.assertEqual(set(voting.voters()), {self.kim, self.sam, leaving, honorary})
        for who in cannot:
            self.assertTrue(voting.why_cant_vote(who), who.status)
        self.assertEqual(
            voting.why_cant_vote(Member.objects.get(pk=storage_only.pk)), "Storage-only accounts can't vote."
        )

    def test_casting_and_changing(self):
        laser = proposal()
        voting.cast(laser, self.kim, "1")
        voting.cast(laser, self.kim, "-1")
        voting.cast(laser, self.sam, "abstain")
        self.assertEqual(Vote.objects.get(member=self.kim).choice, -1)
        sam = Vote.objects.get(member=self.sam)
        self.assertEqual((sam.choice, sam.abstain, sam.label), (None, True, "Abstain"))
        self.assertEqual(Vote.objects.count(), 2)

    def test_only_while_open(self):
        for laser, when in ((proposal(start=1), None), (proposal(start=-5, end=-1), None)):
            with self.assertRaisesMessage(voting.CantVote, "isn't open"):
                voting.cast(laser, self.kim, "1", when)
        last_day = proposal(start=-2, end=0)
        voting.cast(last_day, self.kim, "1", at(TODAY, 23))  # open until the very end of the end date
        with self.assertRaises(voting.CantVote):
            voting.cast(last_day, self.kim, "1", at(TODAY + timedelta(days=1), 0))
        with self.assertRaisesMessage(voting.CantVote, "Please choose"):
            voting.cast(proposal(), self.kim, "2")


class PageTests(ProposalTestCase):
    def test_list(self):
        voted, waiting = proposal(title="Voted"), proposal(title="Waiting")
        proposal(start=2, end=4, title="Later")
        proposal(start=-9, end=-2, title="Done")
        voting.cast(voted, self.kim, "1")
        self.client.force_login(self.kim)
        page = self.client.get("/proposals/")
        self.assertContains(page, "You've voted.")
        self.assertContains(page, "You haven't voted yet.")
        self.assertContains(page, "Coming up")
        self.assertContains(page, "Waiting for the count")
        self.assertNotContains(page, "Write a proposal")
        # The account page lists the one still to vote on.
        account = self.client.get("/account/")
        self.assertContains(account, "Have your say")
        self.assertContains(account, f"/proposals/{waiting.pk}/")
        self.assertNotContains(account, f"/proposals/{voted.pk}/")

    def test_who_sees_proposals(self):
        laser = proposal()
        self.assertRedirects(
            self.client.get("/proposals/"), "/accounts/login/?next=/proposals/", fetch_redirect_response=False
        )
        self.client.force_login(make_member(status=Member.Status.LEFT))
        self.assertEqual(self.client.get(f"/proposals/{laser.pk}/").status_code, 404)
        suspended = make_member(status=Member.Status.SUSPENDED)
        self.client.force_login(suspended)
        page = self.client.get(f"/proposals/{laser.pk}/")
        self.assertContains(page, "Only current members can vote.")
        self.assertNotContains(page, 'name="vote"')
        self.client.post(f"/proposals/{laser.pk}/vote/", {"vote": "1"})
        self.assertFalse(Vote.objects.exists())

    def test_voting_on_the_page(self):
        laser = proposal(description="Cost: **£5,000**. <script>alert(1)</script> [x](javascript:alert(1))")
        self.client.force_login(self.kim)
        page = self.client.get(f"/proposals/{laser.pk}/")
        self.assertContains(page, "<strong>£5,000</strong>")
        self.assertContains(page, "&lt;script&gt;")
        self.assertNotContains(page, "javascript:")
        response = self.client.post(f"/proposals/{laser.pk}/vote/", {"vote": "1"})
        self.assertRedirects(response, f"/proposals/{laser.pk}/")
        page = self.client.get(f"/proposals/{laser.pk}/")
        self.assertContains(page, "You voted <strong>For</strong>")
        self.assertContains(page, "1 member has voted so far")
        # With HTMX, the form swaps itself in place.
        response = self.client.post(f"/proposals/{laser.pk}/vote/", {"vote": "abstain"}, headers={"HX-Request": "true"})
        self.assertContains(response, 'id="vote"')
        self.assertContains(response, "Saved.")
        self.assertNotContains(response, "<html")
        self.assertTrue(Vote.objects.get().abstain)

    def test_the_ballot_is_secret(self):
        laser = proposal()
        voting.cast(laser, self.sam, "-1")
        self.client.force_login(self.kim)
        page = self.client.get(f"/proposals/{laser.pk}/")
        self.assertNotContains(page, "Sam")
        self.assertNotContains(page, "Against</th>")  # no totals while voting is open
        self.assertContains(page, "1 member has voted so far")

    def test_result(self):
        laser = proposal(start=-5, end=-1)
        voting.cast(laser, self.kim, "1", at(TODAY - timedelta(days=2)))
        self.client.force_login(self.kim)
        self.assertContains(self.client.get(f"/proposals/{laser.pk}/"), "will be counted within the hour")
        voting.count(laser.pk)
        page = self.client.get(f"/proposals/{laser.pk}/")
        self.assertContains(page, "<strong>Passed</strong>")
        self.assertContains(page, "You voted For.")
        self.assertContains(self.client.get("/proposals/"), "Passed: 1 for, 0 against, 0 neutral")


class OpeningAndCountingTests(ProposalTestCase):
    def test_opening_tells_voters_once(self):
        make_member(status=Member.Status.LEFT)
        set_quorum(25)
        laser, later = proposal(), proposal(start=1)
        self.assertEqual(voting.update_all(), {"opened": 1, "counted": 0})
        laser.refresh_from_db()
        later.refresh_from_db()
        self.assertEqual((laser.eligible_voters, laser.quorum_percent, laser.quorum_needed), (2, 25, 1))
        self.assertIsNone(later.opened_at)
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), sorted([self.kim.email, self.sam.email]))
        self.assertIn("Buy a new laser", mail.outbox[0].subject)
        self.assertIn(f"/proposals/{laser.pk}/", mail.outbox[0].body)
        self.assertEqual(len(self.discord.messages), 1)
        channel, text = self.discord.messages[0]
        self.assertEqual(channel, discord.FakeDiscord.ANNOUNCEMENT_CHANNEL)
        self.assertIn("New proposal: Buy a new laser", text)
        voting.update_all()
        self.assertEqual((len(mail.outbox), len(self.discord.messages)), (2, 1))

    def test_counting(self):
        laser = proposal(start=-3, end=-1)
        voting.open_voting(laser.pk, at(TODAY - timedelta(days=3)))
        for who, choice in ((self.kim, "1"), (self.sam, "-1"), (make_member(), "1"), (make_member(), "0")):
            voting.cast(laser, who, choice, at(TODAY - timedelta(days=2)))
        voting.cast(laser, make_member(), "abstain", at(TODAY - timedelta(days=2)))
        self.discord.messages.clear()
        self.assertEqual(voting.update_all(), {"opened": 0, "counted": 1})
        laser.refresh_from_db()
        self.assertEqual(
            (laser.votes_for, laser.votes_against, laser.votes_neutral, laser.abstentions, laser.votes_cast),
            (2, 1, 1, 1, 4),
        )
        self.assertEqual((laser.result, laser.quorum_met, laser.passed), (1, True, True))
        self.assertIn("Proposal result: Buy a new laser", self.discord.messages[0][1])
        self.assertIn("Passed: 2 for, 1 against, 1 neutral, 1 abstained", self.discord.messages[0][1])
        self.assertEqual(voting.update_all(), {"opened": 0, "counted": 0})
        self.assertEqual(len(self.discord.messages), 1)

    def test_a_tie_doesnt_pass(self):
        laser = proposal(start=-3, end=-1)
        voting.cast(laser, self.kim, "1", at(TODAY - timedelta(days=2)))
        voting.cast(laser, self.sam, "-1", at(TODAY - timedelta(days=2)))
        voting.count(laser.pk)
        laser.refresh_from_db()
        self.assertFalse(laser.passed)
        self.assertTrue(laser.outcome().startswith("Not passed"))

    def test_quorum(self):
        set_quorum(50)
        for _ in range(4):
            make_member()  # six can vote, so three must
        laser = proposal(start=-3, end=-1)
        voting.open_voting(laser.pk, at(TODAY - timedelta(days=3)))
        voting.cast(laser, self.kim, "1", at(TODAY - timedelta(days=2)))
        voting.cast(laser, self.sam, "abstain", at(TODAY - timedelta(days=2)))
        set_quorum(0)  # too late: the quorum was fixed when voting opened
        voting.count(laser.pk)
        laser.refresh_from_db()
        self.assertEqual((laser.eligible_voters, laser.quorum_needed, laser.turnout), (6, 3, 2))
        self.assertEqual((laser.quorum_met, laser.passed), (False, False))
        self.assertIn("quorum not met: 2 of the 3 needed voted", laser.outcome())

    def test_discord_down_is_tried_again(self):
        proposal()
        self.discord.fail_next = "Discord is down"
        voting.update_all()
        self.assertEqual(self.discord.messages, [])
        self.assertFalse(Proposal.objects.get().opening_posted)
        voting.update_all()
        self.assertEqual(len(self.discord.messages), 1)
        self.assertEqual(len(mail.outbox), 2)  # the emails went the first time, and only then

    def test_dry_run_changes_nothing(self):
        laser = proposal()
        out = StringIO()
        call_command("update_proposals", "--dry-run", stdout=out)
        self.assertIn("would have opened 1", out.getvalue())
        laser.refresh_from_db()
        self.assertIsNone(laser.opened_at)
        self.assertEqual((mail.outbox, self.discord.messages), ([], []))
        call_command("update_proposals", stdout=out)
        laser.refresh_from_db()
        self.assertTrue(laser.opened_at)


class AdminTests(ProposalTestCase):
    def setUp(self):
        super().setUp()
        self.admin = Member.objects.create_superuser(
            "a@example.com", "a-good-password", given_name="A", family_name="D"
        )
        self.client.force_login(self.admin)

    def test_write_a_proposal(self):
        page = self.client.get("/admin/proposals/proposal/add/")
        self.assertContains(page, f'value="{TODAY + timedelta(days=3):%Y-%m-%d}"')
        data = {"title": "Paint", "description": "Blue.", "start_date": TODAY - timedelta(days=1), "end_date": TODAY}
        self.assertContains(self.client.post("/admin/proposals/proposal/add/", data), "can&#x27;t start in the past")
        data["start_date"] = TODAY + timedelta(days=2)
        self.assertContains(self.client.post("/admin/proposals/proposal/add/", data), "on or after the day it starts")
        data["end_date"] = TODAY + timedelta(days=5)
        self.client.post("/admin/proposals/proposal/add/", data)
        self.assertEqual(Proposal.objects.get().created_by, self.admin)
        self.assertContains(self.client.get("/proposals/"), "Write a proposal")

    def test_fixed_once_voting_starts(self):
        later, now = proposal(start=1), proposal()
        self.assertContains(self.client.get(f"/admin/proposals/proposal/{later.pk}/change/"), 'name="title"')
        page = self.client.get(f"/admin/proposals/proposal/{now.pk}/change/")
        self.assertNotContains(page, 'name="title"')
        self.assertEqual(
            self.client.post(f"/admin/proposals/proposal/{now.pk}/delete/", {"post": "yes"}).status_code, 403
        )
        self.assertTrue(Proposal.objects.filter(pk=now.pk).exists())

    def test_shows_who_voted_not_how(self):
        laser = proposal()
        voting.cast(laser, self.kim, "-1")
        page = self.client.get(f"/admin/proposals/proposal/{laser.pk}/change/")
        self.assertContains(page, "Kim Lee")
        self.assertNotContains(page, "Against")
        self.assertContains(self.client.get("/admin/proposals/proposal/"), "Buy a new laser")
