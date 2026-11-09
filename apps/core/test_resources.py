"""Resources and policies (spec 06 §6.8) and feedback (06 §6.9)."""

from django.contrib.auth.models import Group
from django.core import mail
from django.test import TestCase

from apps.equipment.tests.helpers import member
from apps.members.models import Member

from . import policies
from .models import Feedback


class ResourcesTests(TestCase):
    def test_every_policy_is_listed_and_shown_to_anyone(self):
        page = self.client.get("/resources/")
        for name, title in policies.CORE_RULES + policies.OTHER_POLICIES:
            self.assertContains(page, f'href="/resources/policy/{name}/"')
            self.assertContains(page, title)
            response = self.client.get(f"/resources/policy/{name}/")
            self.assertEqual(response.status_code, 200, name)
            self.assertContains(response, "<h1", count=1)  # the policy's own title is the page heading

    def test_unknown_policies_are_not_found(self):
        self.assertEqual(self.client.get("/resources/policy/nothing/").status_code, 404)
        self.assertEqual(self.client.get("/resources/policy/README/").status_code, 404)

    def test_policies_render_their_lists(self):
        self.assertIn("<li>", policies.html("code-of-conduct"))
        self.assertIn("<li>", policies.html("grievance-procedure"))

    def test_the_member_agreement_links_to_the_rules_page(self):
        self.assertContains(self.client.get("/resources/policy/member-agreement/"), 'href="/resources/policy/rules/"')

    def test_the_footer_links_to_them(self):
        self.assertContains(self.client.get("/"), 'href="/resources/"')


class FeedbackTests(TestCase):
    def setUp(self):
        self.trustee = member(given_name="Tru")
        Group.objects.create(name="trustees").user_set.add(self.trustee)
        self.kim = member(given_name="Kim", status=Member.Status.ACTIVE)

    def test_a_member_sends_feedback_to_the_trustees(self):
        self.client.force_login(self.kim)
        response = self.client.post("/feedback/", {"comments": "The lockers page is great."}, follow=True)
        self.assertContains(response, "gone to the trustees")
        (email,) = mail.outbox
        self.assertEqual(email.to, [self.trustee.email])
        self.assertIn("The lockers page is great.", email.body)
        self.assertIn(f"member {self.kim.pk}", email.body)
        self.assertEqual(Feedback.objects.get().member, self.kim)

    def test_comments_are_needed(self):
        self.client.force_login(self.kim)
        response = self.client.post("/feedback/", {"comments": "  "})
        self.assertContains(response, 'aria-invalid="true"')
        self.assertFalse(Feedback.objects.exists())

    def test_only_members_can_send_feedback(self):
        self.assertEqual(self.client.get("/feedback/").status_code, 302)  # log in first
        self.client.force_login(member(status=Member.Status.LEFT))
        self.assertEqual(self.client.post("/feedback/", {"comments": "Hi"}).status_code, 403)
        self.assertFalse(Feedback.objects.exists())
        self.assertEqual(mail.outbox, [])
