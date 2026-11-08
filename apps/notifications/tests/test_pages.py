from django.test import TestCase
from django.urls import reverse

from apps.members.models import Member
from apps.notifications.models import Notification
from apps.notifications.notify import notify


def member(email):
    return Member.objects.create_user(email, "a-good-password", given_name=email.split("@")[0].title())


class NotificationPageTests(TestCase):
    def setUp(self):
        self.kim = member("kim@example.com")
        self.sam = member("sam@example.com")
        self.client.force_login(self.kim)
        self.mine = notify(self.kim, "Pay the <b>deposit</b>", url="/pay/")
        self.theirs = notify(self.sam, "Sam only")

    def test_you_must_log_in(self):
        self.client.logout()
        self.assertRedirects(
            self.client.get(reverse("notifications:list")), f"{reverse('login')}?next={reverse('notifications:list')}"
        )

    def test_the_list_shows_only_your_own_and_escapes_the_text(self):
        response = self.client.get(reverse("notifications:list"))
        self.assertContains(response, "Pay the &lt;b&gt;deposit&lt;/b&gt;")
        self.assertNotContains(response, "Sam only")

    def test_the_header_shows_how_many_are_unread(self):
        self.assertContains(self.client.get(reverse("home")), '<span class="badge" title="Unread">1</span>')
        self.client.post(reverse("notifications:read-all"))
        self.assertNotContains(self.client.get(reverse("home")), 'class="badge"')

    def test_mark_read_and_unread(self):
        self.client.post(reverse("notifications:mark", args=[self.mine.pk, "read"]))
        self.mine.refresh_from_db()
        self.assertFalse(self.mine.unread)
        self.client.post(reverse("notifications:mark", args=[self.mine.pk, "unread"]))
        self.mine.refresh_from_db()
        self.assertTrue(self.mine.unread)

    def test_you_cant_change_someone_elses(self):
        response = self.client.post(reverse("notifications:mark", args=[self.theirs.pk, "read"]))
        self.assertEqual(response.status_code, 404)
        self.theirs.refresh_from_db()
        self.assertTrue(self.theirs.unread)
        self.assertEqual(self.client.get(reverse("notifications:open", args=[self.theirs.pk])).status_code, 404)

    def test_an_unknown_state_is_refused(self):
        response = self.client.post(reverse("notifications:mark", args=[self.mine.pk, "deleted"]))
        self.assertEqual(response.status_code, 404)

    def test_buttons_need_a_post(self):
        response = self.client.get(reverse("notifications:mark", args=[self.mine.pk, "read"]))
        self.assertEqual(response.status_code, 405)
        self.assertEqual(self.client.get(reverse("notifications:read-all")).status_code, 405)

    def test_following_the_link_marks_it_read_and_goes_there(self):
        response = self.client.get(reverse("notifications:open", args=[self.mine.pk]))
        self.assertRedirects(response, "/pay/", fetch_redirect_response=False)
        self.mine.refresh_from_db()
        self.assertFalse(self.mine.unread)

    def test_mark_all_read_leaves_other_members_alone(self):
        self.client.post(reverse("notifications:read-all"))
        self.assertFalse(Notification.objects.get(pk=self.mine.pk).unread)
        self.assertTrue(Notification.objects.get(pk=self.theirs.pk).unread)

    def test_only_unread_filter_and_paging(self):
        for number in range(30):
            notify(self.kim, f"Message {number}")
        self.assertContains(self.client.get(reverse("notifications:list")), "Page 1 of 2")
        self.client.post(reverse("notifications:read-all"))
        response = self.client.get(reverse("notifications:list") + "?show=unread")
        self.assertContains(response, "You've read everything.")
