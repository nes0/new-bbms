"""Trainers marking members trained from the machine's page (spec 05 §5.8; trainers allowed, decided 2026-11-09)."""

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.equipment import pages
from apps.equipment.models import Induction, TrainingCompletion
from apps.members.models import Member
from apps.notifications.models import Notification

from .helpers import laser, member, trained


def waiting(who, machine):
    return Induction.objects.create(member=who, category=machine.induction_key, paid=True)


def trainer(who, machine):
    induction = trained(who, machine)
    induction.is_trainer = True
    induction.save(update_fields=["is_trainer"])
    return who


class SignOffTests(TestCase):
    def setUp(self):
        self.machine = laser()
        self.tess = trainer(member(given_name="Tess"), self.machine)
        self.kim = member(given_name="Kim", family_name="Kay")
        self.induction = waiting(self.kim, self.machine)

    def sign_off(self, who, induction=None):
        self.client.force_login(who)
        url = reverse("equipment:sign-off", args=[self.machine.slug, (induction or self.induction).pk])
        return self.client.post(url, follow=True)

    def test_a_trainer_signs_a_member_off_and_is_recorded_as_their_trainer(self):
        response = self.sign_off(self.tess)
        self.assertContains(response, "Kim K. is signed off on Red laser.")
        self.induction.refresh_from_db()
        self.assertTrue(self.induction.is_trained)
        self.assertEqual(self.induction.trainer, self.tess)
        self.assertTrue(Notification.objects.filter(member=self.kim, kind="induction_trained").exists())

    def test_trainers_see_a_sign_off_button_and_other_members_dont(self):
        self.client.force_login(self.tess)
        self.assertContains(
            self.client.get(reverse("equipment:detail", args=[self.machine.slug])), "Sign off as trained"
        )
        self.client.force_login(trained(member(), self.machine).member)
        self.assertNotContains(
            self.client.get(reverse("equipment:detail", args=[self.machine.slug])), "Sign off as trained"
        )

    def test_the_trainer_goes_back_to_the_waiting_list(self):
        self.client.force_login(self.tess)
        response = self.client.post(reverse("equipment:sign-off", args=[self.machine.slug, self.induction.pk]))
        self.assertRedirects(response, reverse("equipment:detail", args=[self.machine.slug]) + "#sign-off")

    def test_trained_members_who_arent_trainers_cant_sign_off(self):
        sam = trained(member(), self.machine).member
        self.assertContains(self.sign_off(sam), "Only this machine&#x27;s trainers can sign members off.")
        self.induction.refresh_from_db()
        self.assertIsNone(self.induction.trained_at)

    def test_a_trainer_on_another_machine_cant_sign_off_here(self):
        drill = laser(name="Drill", slug="drill", induction_category="drill")
        other = trainer(member(), drill)
        self.assertFalse(pages.can_sign_off(other, self.machine))

    def test_a_trainer_who_isnt_a_current_member_cant_sign_off(self):
        self.tess.status = Member.Status.LEFT
        self.tess.save(update_fields=["status"])
        self.assertFalse(pages.can_sign_off(self.tess, self.machine))

    def test_admins_can_sign_off(self):
        admin = Member.objects.create_superuser("admin@example.com", "a-good-password", status=Member.Status.ACTIVE)
        self.sign_off(admin)
        self.induction.refresh_from_db()
        self.assertEqual(self.induction.trainer, admin)

    def test_nobody_signs_themselves_off(self):
        admin = Member.objects.create_superuser("admin@example.com", "a-good-password", status=Member.Status.ACTIVE)
        admin.email_verified_at = timezone.now()
        admin.save(update_fields=["email_verified_at"])
        own = waiting(admin, self.machine)
        self.client.force_login(admin)
        page = self.client.get(reverse("equipment:detail", args=[self.machine.slug]))
        self.assertContains(page, "You can&#x27;t sign yourself off.")
        with self.assertRaisesMessage(pages.PageError, "You can't sign yourself off."):
            pages.sign_off(self.machine, admin, own.pk)

    def test_the_email_address_must_be_confirmed(self):
        self.kim.email_verified_at = None
        self.kim.save(update_fields=["email_verified_at"])
        self.client.force_login(self.tess)
        page = self.client.get(reverse("equipment:detail", args=[self.machine.slug]))
        self.assertContains(page, "hasn&#x27;t confirmed their email address")
        self.assertContains(self.sign_off(self.tess), "hasn&#x27;t confirmed their email address")
        self.induction.refresh_from_db()
        self.assertIsNone(self.induction.trained_at)

    def test_the_online_course_must_be_finished_first(self):
        self.machine.moodle_course_id = 7
        self.machine.save()
        self.assertContains(self.sign_off(self.tess), "hasn&#x27;t finished the online course")
        TrainingCompletion.objects.create(member=self.kim, moodle_course_id=7, completed_at=timezone.now())
        self.sign_off(self.tess)
        self.induction.refresh_from_db()
        self.assertTrue(self.induction.is_trained)

    def test_unpaid_or_already_trained_members_cant_be_signed_off(self):
        self.induction.paid = False
        self.induction.save(update_fields=["paid"])
        self.assertContains(self.sign_off(self.tess), "hasn&#x27;t paid for training")
        self.induction.paid, self.induction.trained_at = True, timezone.now()
        self.induction.save(update_fields=["paid", "trained_at"])
        self.assertContains(self.sign_off(self.tess), "already trained")
