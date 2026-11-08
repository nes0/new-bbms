from django.contrib.auth.models import Group
from django.test import TestCase

from apps.members.models import Member
from apps.notifications.models import Notification
from apps.notifications.notify import notify, notify_many, notify_role, role_members, unread_count


def member(email, **fields):
    return Member.objects.create_user(email, "a-good-password", given_name=email.split("@")[0].title(), **fields)


class NotifyTests(TestCase):
    def setUp(self):
        self.kim = member("kim@example.com")
        self.sam = member("sam@example.com")

    def test_a_notification_is_added_to_the_members_list(self):
        notification = notify(self.kim, "Hello", kind="greeting", url="/pay/")
        self.assertEqual((notification.member, notification.message, notification.unread), (self.kim, "Hello", True))
        self.assertEqual(unread_count(self.kim), 1)
        self.assertEqual(unread_count(self.sam), 0)

    def test_the_same_key_is_only_sent_once_to_a_member(self):
        self.assertIsNotNone(notify(self.kim, "First", key="trusted_status"))
        self.assertIsNone(notify(self.kim, "Again", key="trusted_status"))
        self.assertIsNotNone(notify(self.sam, "Their own", key="trusted_status"))
        self.assertEqual(Notification.objects.filter(key="trusted_status").count(), 2)
        self.assertEqual(Notification.objects.get(member=self.kim).message, "First")

    def test_without_a_key_the_same_message_can_be_sent_again(self):
        notify(self.kim, "Same")
        notify(self.kim, "Same")
        self.assertEqual(Notification.objects.filter(member=self.kim).count(), 2)

    def test_a_link_must_be_a_page_on_this_site(self):
        for url in ("https://example.com/", "//example.com/", "pay/", "javascript:alert(1)"):
            with self.assertRaises(ValueError, msg=url):
                notify(self.kim, "Hello", url=url)
        self.assertEqual(Notification.objects.count(), 0)

    def test_notify_many_counts_who_was_told(self):
        notify(self.kim, "Hello", key="k")
        self.assertEqual(notify_many([self.kim, self.sam], "Hello", key="k"), 1)

    def test_roles_and_admins(self):
        team = Group.objects.create(name="acs")
        self.kim.groups.add(team)
        boss = member("boss@example.com", is_superuser=True)
        gone = member("gone@example.com", is_active=False)
        gone.groups.add(team)
        admin_role = Group.objects.create(name="admin")
        deputy = member("deputy@example.com")
        deputy.groups.add(admin_role)
        self.assertEqual(set(role_members("acs")), {self.kim})
        self.assertEqual(set(role_members("acs", admins=True)), {self.kim, boss, deputy})
        self.assertEqual(notify_role("acs", message="Quiet", admins=True), 3)
        self.assertFalse(Notification.objects.filter(member__in=[self.sam, gone]).exists())

    def test_a_role_nobody_holds_is_fine(self):
        self.assertEqual(notify_role("nobody", message="Hello"), 0)
