"""The general induction (spec 05 §5.9) and the admin member controls with their audit log (03 §3.9)."""

from datetime import timedelta
from html.parser import HTMLParser

from django.contrib.auth.models import Group, Permission
from django.core import mail
from django.test import TestCase
from django.utils import timezone

from . import changes, induction
from .models import Address, Member, MemberChange

_count = 0


def person(**fields):
    global _count
    _count += 1
    defaults = {
        "given_name": "Kim",
        "family_name": f"Maker{_count}",
        "status": Member.Status.ACTIVE,
        "email_verified_at": timezone.now(),
    }
    defaults.update(fields)
    return Member.objects.create_user(f"person{_count}@example.com", "a-good-password", **defaults)


def comms_member():
    """Someone in the comms role, which may confirm general inductions."""
    comms, _ = Group.objects.get_or_create(name="comms")
    comms.permissions.add(Permission.objects.get(codename="confirm_general_inductions"))
    helper = person(given_name="Ali")
    helper.groups.add(comms)
    return helper


class MemberInductionTests(TestCase):
    def setUp(self):
        self.member = person(status=Member.Status.SETTING_UP)
        self.client.force_login(self.member)

    def test_shows_the_guidance_for_members_and_the_rules(self):
        response = self.client.get("/account/induction/")
        self.assertContains(response, "Guidance for members")
        self.assertContains(response, "return all tools")
        self.assertContains(response, '<h2 id="rules">Rules</h2>', html=True)
        self.assertContains(response, "Don’t be on fire!")

    def test_both_boxes_must_be_ticked(self):
        response = self.client.post("/account/induction/", {"induction_completed": "on"})
        self.assertEqual(response.context["form"].errors.keys(), {"rules_agreed"})
        self.member.refresh_from_db()
        self.assertFalse(self.member.induction_completed)

    def test_doing_it_waits_for_confirmation_and_is_logged(self):
        agreed_at_registration = self.member.rules_agreed_at = timezone.now() - timedelta(days=3)
        self.member.save()
        response = self.client.post("/account/induction/", {"induction_completed": "on", "rules_agreed": "on"})
        self.assertRedirects(response, "/account/")
        self.member.refresh_from_db()
        self.assertTrue(self.member.induction_completed)
        self.assertEqual(self.member.rules_agreed_at, agreed_at_registration)  # kept, as legacy did
        self.assertEqual(self.member.general_induction_status, "waiting")
        self.assertEqual(list(induction.waiting()), [self.member])
        change = MemberChange.objects.get(member=self.member)
        self.assertEqual((change.field, change.old_value, change.new_value), ("induction completed", "no", "yes"))
        self.assertEqual(change.changed_by, self.member)
        self.assertContains(self.client.get("/account/"), "we'll confirm it when you visit")

    def test_a_new_member_is_sent_to_it_from_their_next_steps(self):
        self.assertContains(self.client.get("/account/"), 'href="/account/induction/"')

    def test_doing_it_again_changes_nothing(self):
        induction.complete(self.member)
        self.client.post("/account/induction/", {"induction_completed": "on", "rules_agreed": "on"})
        self.assertEqual(MemberChange.objects.count(), 1)
        self.assertNotContains(self.client.get("/account/induction/"), '<form method="post" class="stack form"')

    def test_needs_a_login(self):
        self.client.logout()
        self.assertEqual(self.client.get("/account/induction/").status_code, 302)

    def test_a_key_holder_is_told_why_the_door_will_not_open(self):
        Member.objects.filter(pk=self.member.pk).update(
            status=Member.Status.ACTIVE, trusted=True, key_holder=True, photo_approved=True
        )
        self.assertContains(self.client.get("/account/"), "until your general induction is confirmed")


class ConfirmingInductionsTests(TestCase):
    def setUp(self):
        self.newcomer = person(status=Member.Status.SETTING_UP)
        induction.complete(self.newcomer)
        self.helper = comms_member()
        self.client.force_login(self.helper)

    def test_the_comms_role_sees_who_is_waiting(self):
        response = self.client.get("/general-inductions/")
        self.assertContains(response, self.newcomer.full_name)
        self.assertContains(response, 'href="/general-inductions/"')  # in the menu

    def test_other_members_may_not(self):
        self.client.force_login(person())
        self.assertEqual(self.client.get("/general-inductions/").status_code, 403)
        self.assertEqual(self.client.post(f"/general-inductions/{self.newcomer.pk}/confirm").status_code, 403)

    def test_confirming_records_who_and_when_and_is_logged(self):
        response = self.client.post(f"/general-inductions/{self.newcomer.pk}/confirm")
        self.assertRedirects(response, "/general-inductions/")
        self.newcomer.refresh_from_db()
        self.assertEqual(self.newcomer.inducted_by, self.helper)
        self.assertIsNotNone(self.newcomer.inducted_at)
        self.assertEqual(self.newcomer.general_induction_status, "confirmed")
        self.assertNotContains(self.client.get("/general-inductions/"), self.newcomer.full_name)
        change = MemberChange.objects.get(member=self.newcomer, changed_by=self.helper)
        self.assertEqual((change.field, change.new_value), ("induction confirmed by", str(self.helper)))

    def test_it_stays_confirmed_if_the_confirmer_is_deleted(self):
        induction.confirm(self.newcomer, self.helper)
        self.helper.delete()
        self.newcomer.refresh_from_db()
        self.assertEqual(self.newcomer.general_induction_status, "confirmed")
        self.assertNotIn(self.newcomer, induction.waiting())

    def test_cannot_confirm_twice_or_before_it_is_done(self):
        induction.confirm(self.newcomer, self.helper)
        response = self.client.post(f"/general-inductions/{self.newcomer.pk}/confirm", follow=True)
        self.assertContains(response, "already been confirmed")
        not_done = person()
        response = self.client.post(f"/general-inductions/{not_done.pk}/confirm", follow=True)
        self.assertContains(response, "done the general induction yet")
        not_done.refresh_from_db()
        self.assertIsNone(not_done.inducted_by)

    def test_members_who_have_left_are_not_listed(self):
        self.newcomer.status = Member.Status.LEFT
        self.newcomer.save()
        self.assertNotIn(self.newcomer, induction.waiting())


class _FormValues(HTMLParser):
    """The values a browser would submit from a page's form with the given id."""

    def __init__(self, form_id):
        super().__init__()
        self.form_id, self.inside, self.values = form_id, False, []
        self._select = self._textarea = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.inside = attrs.get("id") == self.form_id
        if not self.inside:
            return
        if tag == "input" and attrs.get("name") and attrs.get("type") not in ("submit", "button", "file"):
            if attrs.get("type") in ("checkbox", "radio"):
                if "checked" in attrs:
                    self.values.append((attrs["name"], attrs.get("value") or "on"))
            else:
                self.values.append((attrs["name"], attrs.get("value") or ""))
        elif tag == "select":
            self._select = attrs.get("name")
        elif tag == "option" and self._select and "selected" in attrs:
            self.values.append((self._select, attrs.get("value", "")))
        elif tag == "textarea":
            self._textarea = attrs.get("name")
            self.values.append((self._textarea, ""))

    def handle_data(self, data):
        if self._textarea:
            name, value = self.values.pop()
            self.values.append((name, value + data))

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False
        elif tag == "select":
            self._select = None
        elif tag == "textarea":
            self._textarea = None


class AdminMemberControlsTests(TestCase):
    def setUp(self):
        self.admin = Member.objects.create_superuser("admin@example.com", "a-good-password", given_name="Ad")
        self.client.force_login(self.admin)
        self.member = person()
        self.url = f"/admin/members/member/{self.member.pk}/change/"

    def save_admin_page(self, **changes):
        """Submit the member's admin page as it is, with `changes` (True/False tick or untick a box)."""
        parser = _FormValues("member_form")
        parser.feed(self.client.get(self.url).content.decode())
        data = {}
        for name, value in parser.values:
            data.setdefault(name, []).append(value)
        for name, value in changes.items():
            if value is True:
                data[name] = ["on"]
            elif value is False:
                data.pop(name, None)
            else:
                data[name] = [value]
        return self.client.post(self.url, data)

    def test_making_someone_trusted_emails_them_and_is_logged(self):
        response = self.save_admin_page(trusted=True)
        self.assertRedirects(response, "/admin/members/member/")
        self.member.refresh_from_db()
        self.assertTrue(self.member.trusted)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.member.email])
        self.assertIn("trusted member", mail.outbox[0].subject)
        change = MemberChange.objects.get(member=self.member)
        self.assertEqual((change.field, change.old_value, change.new_value), ("trusted", "no", "yes"))
        self.assertEqual(change.changed_by, self.admin)
        self.assertContains(self.client.get(self.url), "Audit log")

    def test_saving_without_changes_logs_nothing_and_sends_nothing(self):
        self.member.trusted = True
        self.member.save()
        self.save_admin_page()
        self.assertEqual(MemberChange.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_photo_and_induction_changes_are_logged(self):
        self.save_admin_page(photo_on_wall=True, induction_completed=True)
        self.assertEqual(
            set(MemberChange.objects.values_list("field", flat=True)), {"photo on wall", "induction completed"}
        )

    def test_a_key_holder_must_be_trusted(self):
        response = self.save_admin_page(key_holder=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Only a trusted member can be a key holder")
        self.member.refresh_from_db()
        self.assertFalse(self.member.key_holder)

    def test_a_key_holder_must_have_had_their_general_induction_confirmed(self):
        self.member.trusted = True
        self.member.save()
        response = self.save_admin_page(key_holder=True)
        self.assertContains(response, "general induction must be confirmed first")
        induction.complete(self.member)
        self.assertContains(self.save_admin_page(key_holder=True), "general induction must be confirmed first")
        induction.confirm(self.member, self.admin)
        self.assertEqual(self.save_admin_page(key_holder=True).status_code, 302)
        self.member.refresh_from_db()
        self.assertTrue(self.member.key_holder)

    def test_an_existing_key_holder_can_still_be_edited(self):
        # e.g. imported from legacy, which didn't need a confirmed induction
        Member.objects.filter(pk=self.member.pk).update(trusted=True, key_holder=True)
        self.assertEqual(self.save_admin_page(notes="Has key 12").status_code, 302)

    def test_approving_a_waiting_address_replaces_the_old_one(self):
        old = Address.objects.create(member=self.member, line_1="1 Old Road", postcode="BN1 1AA", approved=True)
        new = Address.objects.create(member=self.member, line_1="2 New Road", postcode="BN2 2BB")
        index = 0 if self.member.addresses.first() == new else 1
        self.save_admin_page(**{f"addresses-{index}-approved": True})
        self.assertEqual(list(self.member.addresses.all()), [new])
        new.refresh_from_db()
        self.assertTrue(new.approved)
        self.assertFalse(Address.objects.filter(pk=old.pk).exists())

    def test_the_audit_log_cannot_be_edited(self):
        before = changes.snapshot(self.member)
        self.member.banned = True
        self.member.save()
        change = changes.record(self.member, before, changed_by=self.admin)[0]
        self.assertEqual(self.client.get(f"/admin/members/memberchange/{change.pk}/delete/").status_code, 403)
        response = self.client.get(f"/admin/members/memberchange/?member__id__exact={self.member.pk}")
        self.assertContains(response, "banned")

    def test_trusted_members_missing_photos(self):
        missing = person(trusted=True)
        person(trusted=True, photo_on_wall=True)
        person(trusted=True, status=Member.Status.LEFT)
        response = self.client.get("/admin/members/member/?photo=trusted-missing")
        self.assertEqual(list(response.context["cl"].result_list), [missing])

    def test_shortcuts_to_record_a_payment_for_them(self):
        self.assertContains(self.client.get(self.url), f"/admin/payments/payment/record/?member={self.member.pk}")
        response = self.client.get(f"/admin/payments/payment/record/?member={self.member.pk}")
        self.assertEqual(response.context["form"]["member"].value(), str(self.member.pk))


class CommsRoleMigrationTests(TestCase):
    def test_an_existing_comms_role_is_given_the_permission(self):
        from importlib import import_module

        from django.apps import apps

        migration = import_module("apps.members.migrations.0006_general_induction_and_audit_log")
        comms = Group.objects.create(name="comms")
        migration.grant_to_comms(apps, None)
        self.assertTrue(comms.permissions.filter(codename="confirm_general_inductions").exists())
