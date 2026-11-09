"""Group email (spec 06 §6.6): who may email which group (admin rules), who gets it, testing, checking and sending."""

from django.contrib.auth.models import Group
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.billing import emails
from apps.equipment.models import Induction
from apps.equipment.tests.helpers import laser, member, trained
from apps.group_email import audiences
from apps.group_email.models import GroupEmail, SenderRule
from apps.members.models import Member

URL = reverse("group_email:compose")


def keys(who):
    return [choice.key for choice in audiences.allowed(who)]


def rule(**fields):
    return SenderRule.objects.create(**fields)


class WhoMayEmailWhomTests(TestCase):
    def setUp(self):
        self.laser = laser()
        self.team = self.laser.team
        self.comms = Group.objects.create(name="comms")

    def test_an_ordinary_member_may_email_nobody(self):
        self.assertEqual(keys(member()), [])

    def test_admins_may_email_every_group(self):
        admin = member(is_superuser=True, is_staff=True)
        found = keys(admin)
        for key in ("members", "trusted", "key-holders", f"role:{self.comms.pk}", "trained:laser", "waiting:laser"):
            self.assertIn(key, found)

    def test_a_rule_lets_a_role_email_all_members(self):
        rule(sender_role=self.comms, audience=SenderRule.Audience.MEMBERS)
        sender = member()
        sender.groups.add(self.comms)
        self.assertEqual(keys(sender), ["members"])

    def test_a_role_with_no_machine_chosen_emails_about_the_machines_it_looks_after(self):
        rule(sender_role=self.team, audience=SenderRule.Audience.TRAINED)
        laser(name="Blue laser", slug="blue-laser", induction_category="", team=self.comms)
        sender = member()
        sender.groups.add(self.team)
        self.assertEqual(keys(sender), ["trained:laser"])

    def test_every_machine_team_can_be_allowed_with_one_rule(self):
        rule(sender=SenderRule.Sender.MACHINE_TEAM, audience=SenderRule.Audience.WAITING)
        laser(name="Blue laser", slug="blue-laser", induction_category="", team=self.comms)
        sender = member()
        sender.groups.add(self.comms)
        self.assertEqual(keys(sender), ["waiting:blue-laser"])

    def test_a_machines_trainers_can_email_about_the_machines_they_train_on(self):
        rule(sender=SenderRule.Sender.MACHINE_TRAINERS, audience=SenderRule.Audience.TRAINED)
        tess = member()
        Induction.objects.filter(pk=trained(tess, self.laser).pk).update(is_trainer=True)
        self.assertEqual(keys(tess), ["trained:laser"])
        self.assertEqual(keys(trained(member(), self.laser).member), [])

    def test_a_sender_must_be_a_current_member(self):
        rule(sender_role=self.comms, audience=SenderRule.Audience.MEMBERS)
        for fields in ({"status": Member.Status.LEFT}, {"banned": True}):
            sender = member(**fields)
            sender.groups.add(self.comms)
            self.assertEqual(keys(sender), [], fields)

    def test_a_rule_that_doesnt_make_sense_is_refused(self):
        for fields in (
            {"sender": SenderRule.Sender.ROLE, "audience": "members"},  # no role
            {"sender": SenderRule.Sender.MACHINE_TEAM, "audience": "members"},
            {"sender": SenderRule.Sender.ROLE, "sender_role": self.comms, "audience": "role"},  # no receiving role
            {"sender": SenderRule.Sender.ROLE, "sender_role": self.comms, "audience": "members", "category": "laser"},
        ):
            with self.assertRaises(ValidationError, msg=fields):
                SenderRule(**fields).full_clean()


class WhoGetsItTests(TestCase):
    def test_only_current_members_are_in_a_group(self):
        machine = laser()
        kim = member(given_name="Kim")
        trained(kim, machine)
        for fields in ({"status": Member.Status.LEFT}, {"status": Member.Status.SUSPENDED}, {"banned": True}):
            trained(member(**fields), machine)
        waiting = member()
        Induction.objects.create(member=waiting, category="laser", paid=True)
        self.assertEqual(list(audiences.members_of("trained:laser")), [kim])
        self.assertEqual(list(audiences.members_of("waiting:laser")), [waiting])
        self.assertEqual(set(audiences.members_of("members")), {kim, waiting})

    def test_a_key_that_means_nothing_has_nobody_in_it(self):
        member()
        for key in ("", "everyone", "role:x", "trained:", "members:extra"):
            self.assertFalse(audiences.members_of(key).exists(), key)


class PageTests(TestCase):
    def setUp(self):
        self.machine = laser()
        rule(sender_role=self.machine.team, audience=SenderRule.Audience.TRAINED)
        self.sender = member(given_name="Tess", family_name="Team")
        self.sender.groups.add(self.machine.team)
        self.readers = [trained(member(), self.machine).member for _ in range(3)]
        self.client.force_login(self.sender)
        self.form = {"to": "trained:laser", "subject": "Laser tube replaced", "body": "It cuts better now."}

    def test_members_with_no_groups_to_email_dont_see_the_page(self):
        self.client.force_login(member())
        self.assertEqual(self.client.get(URL).status_code, 404)

    def test_the_page_offers_only_the_groups_the_sender_may_email(self):
        response = self.client.get(URL)
        self.assertContains(response, "Members trained on the Red laser")
        self.assertNotContains(response, "All current members")

    def test_a_test_goes_to_the_sender_only(self):
        response = self.client.post(URL, {**self.form, "action": "test"})
        self.assertContains(response, "A test has been sent to you")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.sender.email])
        self.assertEqual(mail.outbox[0].subject, "[Test] Laser tube replaced")
        self.assertIn("This is a test", mail.outbox[0].body)
        self.assertFalse(GroupEmail.objects.exists())

    def test_checking_shows_how_many_will_get_it_and_sends_nothing(self):
        response = self.client.post(URL, {**self.form, "action": "check"})
        self.assertContains(response, "This will go to 3 members")
        self.assertContains(response, "Send it to 3 members")
        self.assertEqual(len(mail.outbox), 0)

    def test_sending_blind_copies_the_group_with_replies_to_the_sender(self):
        response = self.client.post(URL, {**self.form, "action": "send", "checked_to": "trained:laser"}, follow=True)
        self.assertContains(response, "Sent to 3 members")
        [message] = mail.outbox
        self.assertEqual(message.to, [])
        self.assertEqual(sorted(message.bcc), sorted(m.email for m in self.readers))
        self.assertEqual(message.reply_to, [self.sender.email])
        self.assertIn("It cuts better now.", message.body)
        self.assertIn("Sent by Tess Team to members trained on the Red laser", message.body)
        record = GroupEmail.objects.get()
        self.assertEqual((record.sent_by, record.recipients, record.failed), (self.sender, 3, 0))
        self.assertContains(response, "Laser tube replaced")  # in "Sent by you"

    def test_a_big_group_is_sent_in_batches(self):
        for _ in range(60):
            trained(member(), self.machine)
        self.client.post(URL, {**self.form, "action": "send", "checked_to": "trained:laser"})
        self.assertEqual([len(message.bcc) for message in mail.outbox], [50, 13])

    def test_changing_the_group_after_checking_checks_again_instead_of_sending(self):
        rule(sender_role=self.machine.team, audience=SenderRule.Audience.WAITING)
        response = self.client.post(
            URL, {**self.form, "to": "waiting:laser", "action": "send", "checked_to": "trained:laser"}
        )
        self.assertContains(response, "This will go to 0 members")
        self.assertEqual(len(mail.outbox), 0)

    def test_a_group_the_sender_may_not_email_is_refused(self):
        response = self.client.post(URL, {**self.form, "to": "members", "action": "send", "checked_to": "members"})
        self.assertContains(response, "Select a valid choice")
        self.assertEqual(len(mail.outbox), 0)

    def test_nothing_is_sent_during_a_dry_run(self):
        with emails.suppressed():
            self.client.post(URL, {**self.form, "action": "send", "checked_to": "trained:laser"})
        self.assertEqual(len(mail.outbox), 0)


class LinkTests(TestCase):
    def setUp(self):
        self.machine = laser()
        rule(sender=SenderRule.Sender.MACHINE_TEAM, audience=SenderRule.Audience.TRAINED)
        self.sender = member()
        self.sender.groups.add(self.machine.team)

    def test_the_machines_team_sees_a_link_on_its_page_and_others_dont(self):
        page = reverse("equipment:detail", args=[self.machine.slug])
        self.client.force_login(self.sender)
        self.assertContains(self.client.get(page), f"{URL}?to=trained%3Alaser")
        self.assertNotContains(self.client.get(page), "Email everyone waiting")
        self.client.force_login(member())
        self.assertNotContains(self.client.get(page), URL)

    def test_the_account_page_links_to_group_email_for_those_who_can_send(self):
        self.client.force_login(self.sender)
        self.assertContains(self.client.get(reverse("members:account")), "Email a group of members")
        self.client.force_login(member())
        self.assertNotContains(self.client.get(reverse("members:account")), "Email a group of members")

    def test_the_page_starts_with_the_group_from_the_link(self):
        self.client.force_login(self.sender)
        self.assertContains(self.client.get(URL + "?to=trained:laser"), '<option value="trained:laser" selected>')


class AdminTests(TestCase):
    def test_admins_add_a_rule_choosing_from_the_machines(self):
        laser()
        admin = member(is_superuser=True, is_staff=True)
        self.client.force_login(admin)
        page = self.client.get(reverse("admin:group_email_senderrule_add"))
        self.assertContains(page, '<option value="laser">Red laser</option>', html=True)
        response = self.client.post(
            reverse("admin:group_email_senderrule_add"),
            {"sender": "machine-trainers", "audience": "trained", "category": ""},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(str(SenderRule.objects.get()), "Each machine's trainers → members trained on their machines")
