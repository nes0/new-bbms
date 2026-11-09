"""Membership statistics: status history, snapshots and the trend (spec 06 §6.13)."""

from datetime import date, datetime
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import Charge
from apps.equipment.tests.helpers import member

from . import statistics
from .models import Member, MembershipSnapshot, MemberStatusChange

S = Member.Status
TODAY = date(2026, 11, 20)


def at(day):
    return timezone.make_aware(datetime(day.year, day.month, day.day, 12))


class StatusHistoryTests(TestCase):
    def test_every_status_change_is_recorded(self):
        kim = member(status=S.SETTING_UP)
        kim.status = S.ACTIVE
        kim.save(update_fields=["status"])
        kim = Member.objects.get(pk=kim.pk)
        kim.status = S.LEAVING
        kim.save()
        kim.given_name = "Kimberley"
        kim.save()  # no status change: nothing recorded
        kim.save(update_fields=["given_name"])
        changes = [(c.old_status, c.new_status) for c in kim.status_changes.order_by("pk")]
        self.assertEqual(changes, [("", "setting-up"), ("setting-up", "active"), ("active", "leaving")])


class StatisticsTests(TestCase):
    def setUp(self):
        localdate = mock.patch("django.utils.timezone.localdate", return_value=TODAY)
        localdate.start()
        self.addCleanup(localdate.stop)

    def join(self, when, status=S.ACTIVE):
        who = member(status=S.SETTING_UP)
        who.status = status
        who.save()
        who.status_changes.filter(new_status=status).update(changed_at=at(when))
        return who

    def paid(self, who, *days):
        for day in days:
            Charge.objects.create(member=who, charge_date=day, status=Charge.Status.PAID, amount_pence=2000)

    def test_totals_now_and_this_months_joiners_and_leavers(self):
        self.join(date(2026, 11, 3))
        self.join(date(2026, 10, 3))
        gone = self.join(date(2026, 9, 1))
        gone.status = S.LEFT
        gone.save()
        honorary = member(status=S.HONORARY)  # made honorary straight away: a joiner too, but last month
        honorary.status_changes.update(changed_at=at(date(2026, 10, 1)))
        now = statistics.now()
        by_status = dict(now["by_status"])
        self.assertEqual((by_status["Active"], by_status["Honorary"], now["members"]), (2, 1, 2))
        self.assertEqual((now["joined"], now["left"]), (1, 1))

    def test_the_nightly_snapshot(self):
        member(status=S.ACTIVE)
        call_command("snapshot_members", stdout=mock.Mock())
        call_command("snapshot_members", stdout=mock.Mock())  # again the same day: updated, not doubled
        snapshot = MembershipSnapshot.objects.get()
        self.assertEqual((snapshot.date, snapshot.counts.get("active")), (TODAY, 1))

    def test_months_before_the_first_snapshot_are_estimated_from_payments(self):
        stayed, went = member(status=S.ACTIVE), member(status=S.LEFT)
        MemberStatusChange.objects.all().delete()  # as if imported: legacy had no history
        self.paid(stayed, date(2026, 8, 5), date(2026, 9, 5), date(2026, 10, 5))
        self.paid(went, date(2026, 9, 9))
        MembershipSnapshot.objects.create(date=date(2026, 10, 31), counts={"active": 1, "leaving": 0})
        rows = {row["month"]: row for row in statistics.trend()}
        self.assertEqual(list(rows), ["2026-08-01", "2026-09-01", "2026-10-01", "2026-11-01"])
        aug, sep, oct_, nov = rows.values()
        self.assertEqual((aug["estimated"], aug["members"], aug["joined"]), (True, 1, 1))
        self.assertEqual((sep["members"], sep["joined"], sep["left"]), (2, 1, 0))
        self.assertEqual((oct_["estimated"], oct_["members"]), (False, 1))  # from the snapshot
        self.assertEqual(oct_["left"], 0)  # recorded months use recorded changes, not estimates
        self.assertEqual((nov["estimated"], nov["members"]), (False, 1))  # this month: live

    def test_recorded_months_count_status_changes(self):
        MembershipSnapshot.objects.create(date=date(2026, 9, 30), counts={"active": 0})
        self.join(date(2026, 10, 2))
        self.join(date(2026, 10, 9), status=S.HONORARY)
        rows = {row["month"]: row for row in statistics.trend()}
        self.assertEqual(rows["2026-10-01"]["joined"], 2)
        self.assertFalse(rows["2026-09-01"]["estimated"])

    def test_the_stats_page_shows_them_to_members(self):
        self.client.force_login(member(status=S.ACTIVE))
        page = self.client.get("/stats/")
        self.assertContains(page, "Members now")
        self.assertContains(page, 'id="trend-data"')
        self.assertContains(page, "The figures as a table")
