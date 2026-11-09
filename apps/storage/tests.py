"""Lockers: prices by size and location, overrides, availability, rentals and limits (spec 06 §6.2)."""

from django.test import TestCase

from apps.equipment.tests.helpers import member
from apps.members.models import Member
from apps.site_settings.models import SiteSettings

from . import rules
from .models import Locker, LockerChange, LockerRent, LockerRental, LockerSize, MemberLockerLimit, StorageLocation


class LockerTestCase(TestCase):
    def setUp(self):
        self.trustee = Member.objects.create_superuser(
            "trustee@example.com", "a-good-password", given_name="T", family_name="Rustee"
        )
        self.workshop = StorageLocation.objects.create(name="Workshop wall")
        self.office = StorageLocation.objects.create(name="Office")
        self.large = LockerSize.objects.create(name="Large")
        self.small = LockerSize.objects.create(name="Small")
        LockerRent.objects.create(location=self.workshop, size=self.large, monthly_rent_pence=800)
        LockerRent.objects.create(location=self.workshop, size=self.small, monthly_rent_pence=400)
        LockerRent.objects.create(location=self.office, size=self.large, monthly_rent_pence=1000)
        self.kim = member(given_name="Kim")
        self.sam = member(given_name="Sam")

    def locker(self, number, location=None, size=None, **fields):
        return Locker.objects.create(
            location=location or self.workshop, number=number, size=size or self.large, **fields
        )


class PriceTests(LockerTestCase):
    def test_default_rent_depends_on_size_and_location(self):
        self.assertEqual(rules.advertised_rent_pence(self.locker(1)), 800)
        self.assertEqual(rules.advertised_rent_pence(self.locker(2, size=self.small)), 400)
        self.assertEqual(rules.advertised_rent_pence(self.locker(3, location=self.office)), 1000)

    def test_a_locker_with_no_default_rent_is_not_offered(self):
        locker = self.locker(4, location=self.office, size=self.small)
        self.assertEqual(rules.status(locker), rules.NO_PRICE)
        self.assertNotIn(locker, [locker for locker, _ in rules.available_lockers()])
        with self.assertRaises(rules.LockerError):
            rules.start_rental(locker, self.kim, self.trustee)

    def test_an_advertised_override_is_shown_and_becomes_the_renters_price(self):
        locker = self.locker(5)
        rules.set_advertised_rent(locker, 500, "Door sticks", self.trustee)
        self.assertIn((locker, 500), [(lk, price) for lk, price in rules.available_lockers()])
        rental = rules.start_rental(locker, self.kim, self.trustee)
        self.assertEqual((rental.rent_override_pence, rental.rent_override_reason), (500, "Door sticks"))
        self.assertEqual(rules.rental_rent_pence(rental), 500)
        locker.refresh_from_db()
        self.assertIsNone(locker.advertised_rent_pence)  # moved onto the rental

    def test_an_override_can_be_free(self):
        locker = rules.set_advertised_rent(self.locker(6), 0, "Trial", self.trustee)
        self.assertEqual(rules.advertised_rent_pence(locker), 0)
        self.assertEqual(rules.status(locker), rules.AVAILABLE)

    def test_an_advertised_override_needs_a_reason_and_an_empty_locker(self):
        locker = self.locker(7)
        with self.assertRaises(rules.LockerError):
            rules.set_advertised_rent(locker, 500, "", self.trustee)
        rules.start_rental(locker, self.kim, self.trustee)
        with self.assertRaises(rules.LockerError):
            rules.set_advertised_rent(locker, 500, "Cheaper", self.trustee)

    def test_what_one_member_pays_can_be_changed_and_ends_with_their_rental(self):
        locker = self.locker(8)
        rental = rules.start_rental(locker, self.kim, self.trustee)
        rules.set_rental_rent(rental, 300, "Hardship", self.trustee)
        rental.refresh_from_db()
        self.assertEqual(rules.rental_rent_pence(rental), 300)
        rules.end_rental(rental, self.trustee, "Moved away")
        next_rental = rules.start_rental(locker, self.sam, self.trustee)
        self.assertEqual(rules.rental_rent_pence(next_rental), 800)  # the next renter pays the default

    def test_changing_what_a_member_pays_needs_a_reason(self):
        rental = rules.start_rental(self.locker(9), self.kim, self.trustee)
        with self.assertRaises(rules.LockerError):
            rules.set_rental_rent(rental, 300, "", self.trustee)

    def test_renters_without_an_override_follow_a_lower_default_at_once(self):
        rental = rules.start_rental(self.locker(10), self.kim, self.trustee)
        rent = LockerRent.objects.get(location=self.workshop, size=self.large)
        rent.monthly_rent_pence = 700
        rent.save()
        rules.default_rent_changed(rent, 800, self.trustee)
        rental.refresh_from_db()
        self.assertEqual(rules.rental_rent_pence(rental), 700)  # a higher one waits for notice: test_renting.py


class AvailabilityTests(LockerTestCase):
    def test_reserved_and_out_of_use_lockers_are_not_offered_or_rentable(self):
        locker = self.locker(1)
        for hold in (Locker.Hold.RESERVED, Locker.Hold.OUT_OF_USE):
            rules.set_availability(locker, hold, "Trustees' tools", self.trustee)
            locker.refresh_from_db()
            self.assertEqual(rules.status(locker), hold)
            self.assertEqual(rules.available_lockers(), [])
            with self.assertRaises(rules.LockerError):
                rules.start_rental(locker, self.kim, self.trustee)
        rules.set_availability(locker, Locker.Hold.NONE, "", self.trustee)
        locker.refresh_from_db()
        self.assertEqual(rules.status(locker), rules.AVAILABLE)

    def test_availability_needs_a_note_and_an_empty_locker(self):
        locker = self.locker(2)
        with self.assertRaises(rules.LockerError):
            rules.set_availability(locker, Locker.Hold.RESERVED, "", self.trustee)
        rules.start_rental(locker, self.kim, self.trustee)
        with self.assertRaises(rules.LockerError):
            rules.set_availability(locker, Locker.Hold.OUT_OF_USE, "Broken", self.trustee)

    def test_a_rented_locker_is_not_offered_and_comes_back_when_given_up(self):
        locker = self.locker(3)
        rental = rules.start_rental(locker, self.kim, self.trustee)
        self.assertEqual(rules.status(locker), rules.RENTED)
        self.assertEqual(rules.available_lockers(), [])
        with self.assertRaises(rules.LockerError):
            rules.start_rental(locker, self.sam, self.trustee)
        rules.end_rental(rental, self.trustee)
        self.assertEqual(rules.status(locker), rules.AVAILABLE)

    def test_never_rented_lockers_are_offered(self):
        self.locker(4)
        self.assertEqual(len(rules.available_lockers()), 1)

    def test_ending_twice_changes_nothing(self):
        rental = rules.start_rental(self.locker(5), self.kim, self.trustee)
        rules.end_rental(rental, self.trustee, "first")
        rules.end_rental(rental, self.trustee, "second")
        rental.refresh_from_db()
        self.assertEqual(rental.end_reason, "first")
        self.assertEqual(LockerChange.objects.filter(what="ended").count(), 1)


class LimitTests(LockerTestCase):
    def test_members_may_rent_up_to_the_setting(self):
        rules.start_rental(self.locker(1), self.kim, self.trustee)
        second = self.locker(2)
        with self.assertRaises(rules.LockerError):
            rules.start_rental(second, self.kim, self.trustee)
        SiteSettings.objects.update_or_create(pk=1, defaults={"lockers_per_member": 2})
        rules.start_rental(second, self.kim, self.trustee)

    def test_a_trustee_can_give_one_member_a_different_limit(self):
        rules.set_member_limit(self.kim, 3, "Runs the bike workshop", self.trustee)
        self.assertEqual(rules.locker_limit(self.kim), 3)
        self.assertEqual(rules.locker_limit(self.sam), 1)
        rules.set_member_limit(self.kim, None, "Workshop closed", self.trustee)
        self.assertEqual(rules.locker_limit(self.kim), 1)
        changes = list(LockerChange.objects.filter(what="member-limit").order_by("pk"))
        self.assertEqual([(c.old_value, c.new_value) for c in changes], [("usual (1)", "3"), ("3", "usual (1)")])

    def test_former_members_cannot_rent(self):
        self.kim.status = Member.Status.LEFT
        self.kim.save()
        with self.assertRaises(rules.LockerError):
            rules.start_rental(self.locker(3), self.kim, self.trustee)


class LogTests(LockerTestCase):
    def test_every_change_is_logged_with_who_and_why(self):
        locker = self.locker(1)
        rules.set_advertised_rent(locker, 600, "Dent in door", self.trustee)
        rules.set_availability(locker, Locker.Hold.RESERVED, "Hold for the fair", self.trustee)
        rules.set_availability(locker, Locker.Hold.NONE, "", self.trustee)
        rental = rules.start_rental(locker, self.kim, self.trustee)
        rules.set_rental_rent(rental, None, "Back to normal", self.trustee)
        rules.end_rental(rental, self.trustee, "Gave it up")
        whats = list(LockerChange.objects.order_by("pk").values_list("what", flat=True))
        self.assertEqual(whats, ["advertised-rent", "availability", "availability", "rented", "rental-rent", "ended"])
        first = LockerChange.objects.order_by("pk").first()
        self.assertEqual((first.old_value, first.new_value, first.reason), ("default", "£6.00", "Dent in door"))
        self.assertTrue(all(c.changed_by == self.trustee for c in LockerChange.objects.all()))


class AdminTests(LockerTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.trustee)

    def test_default_rents_are_entered_in_pounds_and_logged(self):
        rent = LockerRent.objects.get(location=self.workshop, size=self.large)
        response = self.client.post(
            f"/admin/storage/lockerrent/{rent.pk}/change/",
            {"location": self.workshop.pk, "size": self.large.pk, "monthly_rent": "8.50", "reason": "Inflation"},
        )
        self.assertEqual(response.status_code, 302)
        rent.refresh_from_db()
        self.assertEqual(rent.monthly_rent_pence, 850)
        change = LockerChange.objects.get(what="default-rent")
        self.assertEqual((change.old_value, change.new_value, change.reason), ("£8.00", "£8.50", "Inflation"))

    def locker_form(self, locker, **changes):
        data = {
            "location": locker.location_id,
            "number": locker.number,
            "size": locker.size_id,
            "hold": locker.hold,
            "hold_note": locker.hold_note,
            "advertised_rent": "",
            "change_reason": "",
            "rentals-TOTAL_FORMS": "0",
            "rentals-INITIAL_FORMS": "0",
        }
        data.update(changes)
        return self.client.post(f"/admin/storage/locker/{locker.pk}/change/", data)

    def test_overriding_an_empty_lockers_price_and_availability_in_the_admin(self):
        locker = self.locker(1)
        self.assertEqual(self.locker_form(locker, advertised_rent="5.00").status_code, 200)  # needs a reason
        self.assertEqual(self.locker_form(locker, advertised_rent="5.00", change_reason="Dented").status_code, 302)
        locker.refresh_from_db()
        self.assertEqual(locker.advertised_rent_pence, 500)
        self.assertEqual(self.locker_form(locker, hold="reserved").status_code, 200)  # needs a note
        self.locker_form(locker, hold="reserved", hold_note="Fair", advertised_rent="5.00", change_reason="Dented")
        locker.refresh_from_db()
        self.assertEqual((locker.hold, locker.hold_note), ("reserved", "Fair"))
        self.assertEqual(LockerChange.objects.filter(what="availability").count(), 1)

    def test_a_new_locker_needs_a_default_rent_for_its_size_and_location(self):
        response = self.client.post(
            "/admin/storage/locker/add/",
            {
                "location": self.office.pk,
                "number": 1,
                "size": self.small.pk,
                "hold": "",
                "rentals-TOTAL_FORMS": "0",
                "rentals-INITIAL_FORMS": "0",
            },
        )
        self.assertContains(response, "Set the default rent for Small lockers in Office first")
        self.assertFalse(Locker.objects.exists())

    def test_a_rented_lockers_price_and_availability_cannot_be_changed_there(self):
        locker = self.locker(2)
        rules.start_rental(locker, self.kim, self.trustee)
        self.locker_form(locker, hold="out_of_use", hold_note="Broken", advertised_rent="1.00", change_reason="x")
        locker.refresh_from_db()
        self.assertEqual((locker.hold, locker.advertised_rent_pence), ("", None))

    def test_renting_changing_the_rent_and_ending_a_rental_in_the_admin(self):
        locker = self.locker(3)
        response = self.client.post("/admin/storage/lockerrental/add/", {"locker": locker.pk, "member": self.kim.pk})
        self.assertEqual(response.status_code, 302)
        rental = LockerRental.objects.get()
        self.assertEqual(rental.started_by, self.trustee)
        url = f"/admin/storage/lockerrental/{rental.pk}/change/"
        self.assertEqual(self.client.post(url, {"rent_override": "2.00"}).status_code, 200)  # needs a reason
        self.client.post(url, {"rent_override": "2.00", "reason": "Volunteer"})
        rental.refresh_from_db()
        self.assertEqual(rental.rent_override_pence, 200)
        self.client.post(url, {"rent_override": "2.00", "reason": "Volunteer", "end_now": "on", "end_reason": "Done"})
        rental.refresh_from_db()
        self.assertFalse(rental.is_open)
        self.assertEqual(rental.end_reason, "Done")

    def test_renting_over_the_limit_is_refused_on_the_form(self):
        rules.start_rental(self.locker(4), self.kim, self.trustee)
        response = self.client.post(
            "/admin/storage/lockerrental/add/", {"locker": self.locker(5).pk, "member": self.kim.pk}
        )
        self.assertContains(response, "their limit")
        self.assertEqual(LockerRental.objects.count(), 1)

    def test_member_limits_are_set_and_removed_in_the_admin_and_logged(self):
        self.client.post(
            "/admin/storage/memberlockerlimit/add/", {"member": self.kim.pk, "limit": "2", "reason": "Two projects"}
        )
        self.assertEqual(rules.locker_limit(self.kim), 2)
        limit = MemberLockerLimit.objects.get()
        self.assertEqual(limit.set_by, self.trustee)
        self.client.post(f"/admin/storage/memberlockerlimit/{limit.pk}/delete/", {"post": "yes"})
        self.assertEqual(rules.locker_limit(self.kim), 1)
        self.assertEqual(LockerChange.objects.filter(what="member-limit").count(), 2)

    def test_the_locker_list_shows_holders_and_marks_overrides(self):
        rented = self.locker(6)
        rental = rules.start_rental(rented, self.kim, self.trustee)
        rules.set_rental_rent(rental, 100, "Helper", self.trustee)
        empty = self.locker(7)
        rules.set_advertised_rent(empty, 700, "Old", self.trustee)
        response = self.client.get("/admin/storage/locker/")
        self.assertContains(response, "£1.00 (override)")
        self.assertContains(response, "£7.00 (override)")
        self.assertContains(response, str(self.kim))

    def test_filter_for_lockers_held_by_members_who_have_left(self):
        rules.start_rental(self.locker(8), self.kim, self.trustee)
        rules.start_rental(self.locker(9), self.sam, self.trustee)
        Member.objects.filter(pk=self.kim.pk).update(status=Member.Status.LEFT)
        response = self.client.get("/admin/storage/locker/?status=left")
        self.assertContains(response, "Workshop wall locker 8")
        self.assertNotContains(response, "Workshop wall locker 9")

    def test_a_locker_with_rental_history_cannot_be_deleted(self):
        locker = self.locker(10)
        rules.end_rental(rules.start_rental(locker, self.kim, self.trustee), self.trustee)
        self.assertEqual(self.client.get(f"/admin/storage/locker/{locker.pk}/delete/").status_code, 403)

    def test_every_admin_page_opens(self):
        locker = self.locker(11)
        rental = rules.start_rental(locker, self.kim, self.trustee)
        rules.set_member_limit(self.kim, 2, "Two projects", self.trustee)
        pages = [
            "/admin/storage/",
            "/admin/storage/storagelocation/add/",
            "/admin/storage/lockersize/add/",
            "/admin/storage/lockerrent/",
            "/admin/storage/lockerrent/add/",
            "/admin/storage/locker/",
            "/admin/storage/locker/add/",
            f"/admin/storage/locker/{locker.pk}/change/",
            "/admin/storage/lockerrental/",
            "/admin/storage/lockerrental/add/",
            f"/admin/storage/lockerrental/{rental.pk}/change/",
            "/admin/storage/memberlockerlimit/",
            "/admin/storage/lockerchange/",
        ]
        for page in pages:
            self.assertEqual(self.client.get(page).status_code, 200, page)

    def test_a_members_own_limit_shows_on_their_admin_page(self):
        rules.set_member_limit(self.kim, 2, "Two projects", self.trustee)
        self.assertContains(self.client.get(f"/admin/members/member/{self.kim.pk}/change/"), "Two projects")


class MembersPageTests(LockerTestCase):
    def test_shows_available_lockers_by_location_with_prices(self):
        self.locker(1)
        self.locker(2, location=self.office)
        held = self.locker(3, hold=Locker.Hold.OUT_OF_USE, hold_note="Broken")
        self.client.force_login(self.kim)
        response = self.client.get("/lockers/")
        self.assertContains(response, "Workshop wall")
        self.assertContains(response, "£8.00")
        self.assertContains(response, "£10.00")
        self.assertNotContains(response, f"<td>{held.number}</td>")
        self.assertContains(response, "up to 1 locker")

    def test_shows_the_members_own_lockers_and_what_they_pay(self):
        rental = rules.start_rental(self.locker(4), self.kim, self.trustee)
        rules.set_rental_rent(rental, 250, "Helper", self.trustee)
        self.client.force_login(self.kim)
        response = self.client.get("/lockers/")
        self.assertContains(response, "Workshop wall locker 4")
        self.assertContains(response, "£2.50 a month")

    def test_needs_a_login(self):
        self.assertEqual(self.client.get("/lockers/").status_code, 302)
