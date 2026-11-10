"""Expenses and reimbursement (spec 06 §6.4)."""

import io
import tempfile
from datetime import timedelta

from django.contrib.auth.models import Group, Permission
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from apps.equipment.tests.helpers import member as make_member
from apps.expenses import claims
from apps.expenses.models import Expense
from apps.members.models import Member
from apps.notifications.models import Notification
from apps.payments.credit import credit_pence
from apps.site_settings.models import SiteSettings

TODAY = timezone.localdate()


def photo(fmt="JPEG", name="receipt.jpg", size=(400, 600), exif=True):
    out = io.BytesIO()
    image = Image.new("RGB", size, (250, 250, 250))
    extra = {}
    if exif and fmt == "JPEG":
        data = Image.Exif()
        data[0x010F] = "MadeUpCam"  # camera make: should not survive
        extra["exif"] = data.tobytes()
    image.save(out, fmt, **extra)
    return SimpleUploadedFile(name, out.getvalue(), content_type=f"image/{fmt.lower()}")


def pdf(name="receipt.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4\n% a made-up receipt\n", content_type="application/pdf")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="bbms-test-media-"))
class ExpenseTestCase(TestCase):
    def setUp(self):
        self.kim = make_member(given_name="Kim", family_name="Lee")
        self.trustee = Member.objects.create_superuser(
            "trustee@example.com", "a-good-password", given_name="Tess", family_name="Trustee"
        )
        self.finance = make_member(given_name="Fin")
        group = Group.objects.create(name="finance")
        group.permissions.add(Permission.objects.get(codename="see_all_expenses"))
        group.user_set.add(self.finance)

    def claim(self, who=None, receipt=None, **fields):
        data = {
            "category": "tools",
            "description": "A new saw blade",
            "amount": "12.50",
            "spent_on": TODAY - timedelta(days=2),
            "receipt": receipt or photo(),
            **fields,
        }
        self.client.force_login(who or self.kim)
        return self.client.post("/expenses/claim/", data)

    def waiting(self, who=None, amount=1250):
        who = who or self.kim
        return claims.submit(
            who,
            category="tools",
            description="Saw blade",
            amount_pence=amount,
            spent_on=TODAY,
            receipt=claims.receipt_file(pdf()),
        )


class ClaimTests(ExpenseTestCase):
    def test_claim(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.claim()
        expense = Expense.objects.get()
        self.assertRedirects(response, f"/expenses/{expense.pk}/")
        self.assertEqual((expense.member, expense.amount_pence, expense.status), (self.kim, 1250, "waiting"))
        with expense.receipt.open("rb") as file:
            kept = Image.open(io.BytesIO(file.read()))
            self.assertEqual(kept.format, "JPEG")
            self.assertNotIn(0x010F, kept.getexif())  # the camera's metadata is gone
        # The trustee is told, in BBMS and by email; the finance role only sees.
        note = Notification.objects.get(member=self.trustee)
        self.assertEqual(note.url, f"/expenses/{expense.pk}/")
        self.assertIn("Kim Lee claimed £12.50", note.message)
        self.assertEqual([m.to for m in mail.outbox], [[self.trustee.email]])
        self.assertIn(f"/expenses/{expense.pk}/", mail.outbox[0].body)

    def test_pdf_and_png(self):
        self.claim(receipt=pdf())
        self.claim(receipt=photo("PNG", "r.png"))
        names = sorted(e.receipt.name.rsplit(".", 1)[1] for e in Expense.objects.all())
        self.assertEqual(names, ["pdf", "png"])

    def test_checks(self):
        self.assertContains(self.claim(amount="1000.01"), "You can claim up to £1,000.00")
        config = SiteSettings.load()
        config.expense_maximum_pence = 5000
        config.save()
        self.assertContains(self.claim(amount="50.01"), "You can claim up to £50.00")
        self.assertContains(self.claim(spent_on=TODAY + timedelta(days=1)), "can&#x27;t be in the future")
        fake = SimpleUploadedFile("r.pdf", b"<html>not a receipt</html>", content_type="application/pdf")
        self.assertContains(self.claim(receipt=fake), "doesn&#x27;t look like a receipt")
        self.assertContains(self.claim(receipt=photo("GIF", "r.gif")), "JPEG, PNG or PDF")
        self.assertContains(self.claim(category=""), "This field is required")
        self.assertFalse(Expense.objects.exists())

    def test_only_members_claim(self):
        self.client.force_login(make_member(status=Member.Status.LEFT))
        self.assertEqual(self.client.get("/expenses/claim/").status_code, 404)
        self.assertEqual(self.client.get("/expenses/").status_code, 404)


class WhoSeesWhatTests(ExpenseTestCase):
    def test_members_see_their_own(self):
        mine, theirs = self.waiting(), self.waiting(make_member(given_name="Other"))
        self.client.force_login(self.kim)
        page = self.client.get("/expenses/")
        self.assertContains(page, f"/expenses/{mine.pk}/")
        self.assertNotContains(page, f"/expenses/{theirs.pk}/")
        self.assertNotContains(page, 'name="member"')
        self.assertEqual(self.client.get(f"/expenses/{mine.pk}/receipt/").status_code, 200)
        for url in (f"/expenses/{theirs.pk}/", f"/expenses/{theirs.pk}/receipt/"):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        self.assertEqual(self.client.get(f"/expenses/?member={theirs.member_id}").context["page"].paginator.count, 1)

    def test_finance_sees_everyone_but_cant_decide(self):
        expense = self.waiting()
        self.client.force_login(self.finance)
        self.assertContains(self.client.get("/expenses/"), "Kim Lee")
        page = self.client.get(f"/expenses/{expense.pk}/")
        self.assertNotContains(page, 'value="approve"')
        receipt = self.client.get(f"/expenses/{expense.pk}/receipt/")
        self.assertEqual(receipt["Content-Type"], "application/pdf")
        self.assertEqual(receipt["Content-Security-Policy"], "sandbox")
        self.client.post(f"/expenses/{expense.pk}/decide/", {"decision": "approve"})
        expense.refresh_from_db()
        self.assertEqual(expense.status, "waiting")

    def test_filters_and_sorting(self):
        small, big = self.waiting(amount=100), self.waiting(amount=9000)
        claims.decline(small.pk, self.trustee, "Not ours")
        self.client.force_login(self.trustee)
        page = self.client.get("/expenses/?status=waiting")
        self.assertEqual(list(page.context["page"]), [big])
        page = self.client.get("/expenses/?sort=biggest")
        self.assertEqual(list(page.context["page"]), [big, small])
        self.assertContains(self.client.get("/"), "Expenses")


class DecisionTests(ExpenseTestCase):
    def test_approve_pays_into_credit_once(self):
        expense = self.waiting()
        self.client.force_login(self.trustee)
        page = self.client.get(f"/expenses/{expense.pk}/")
        self.assertContains(page, "Approve and add £12.50")
        self.client.post(f"/expenses/{expense.pk}/decide/", {"decision": "approve"})
        expense.refresh_from_db()
        self.assertEqual((expense.status, expense.decided_by), ("approved", self.trustee))
        self.assertEqual((expense.payment.source, expense.payment.reference), ("reimbursement", str(expense.pk)))
        self.assertEqual(credit_pence(self.kim), 1250)
        self.assertTrue(Notification.objects.filter(member=self.kim, key=f"{expense.pk}-expense_approved").exists())
        self.assertIn("approved", mail.outbox[-1].subject)
        # A second click (or a second admin) doesn't pay twice.
        response = self.client.post(f"/expenses/{expense.pk}/decide/", {"decision": "approve"}, follow=True)
        self.assertContains(response, "already been approved")
        with self.assertRaises(claims.CantDecide):
            claims.decline(expense.pk, self.trustee, "Oops")
        self.assertEqual(credit_pence(self.kim), 1250)

    def test_nobody_approves_their_own(self):
        own = self.waiting(self.trustee)
        with self.assertRaisesMessage(claims.CantDecide, "Someone else"):
            claims.approve(own.pk, self.trustee)
        self.client.force_login(self.trustee)
        self.assertNotContains(self.client.get(f"/expenses/{own.pk}/"), 'value="approve"')

    def test_given_the_permission_by_a_role(self):
        treasurer = make_member(given_name="Tre")
        role = Group.objects.create(name="treasurer")
        role.permissions.add(Permission.objects.get(codename="approve_expense"))
        role.user_set.add(treasurer)
        self.assertIn(treasurer, claims.approvers())
        claims.approve(self.waiting().pk, Member.objects.get(pk=treasurer.pk))
        self.assertEqual(credit_pence(self.kim), 1250)

    def test_decline_needs_a_reason(self):
        expense = self.waiting()
        self.client.force_login(self.trustee)
        response = self.client.post(f"/expenses/{expense.pk}/decide/", {"decision": "decline"}, follow=True)
        self.assertContains(response, "Please say why")
        self.client.post(f"/expenses/{expense.pk}/decide/", {"decision": "decline", "reason": "No receipt total"})
        expense.refresh_from_db()
        self.assertEqual(
            (expense.status, expense.decline_reason, expense.payment), ("declined", "No receipt total", None)
        )
        self.assertEqual(credit_pence(self.kim), 0)
        self.assertIn("No receipt total", mail.outbox[-1].body)
        self.client.force_login(self.kim)
        self.assertContains(self.client.get(f"/expenses/{expense.pk}/"), "No receipt total")

    def test_withdraw(self):
        expense = self.waiting()
        storage, name = expense.receipt.storage, expense.receipt.name
        self.client.force_login(self.kim)
        self.assertRedirects(self.client.post(f"/expenses/{expense.pk}/withdraw/"), "/expenses/")
        self.assertFalse(Expense.objects.exists())
        self.assertFalse(storage.exists(name))
        approved = claims.approve(self.waiting().pk, self.trustee)
        self.client.post(f"/expenses/{approved.pk}/withdraw/")
        self.assertTrue(Expense.objects.filter(pk=approved.pk).exists())

    def test_admin_is_a_record(self):
        expense = self.waiting()
        self.client.force_login(self.trustee)
        self.assertContains(self.client.get("/admin/expenses/expense/"), "£12.50")
        page = self.client.get(f"/admin/expenses/expense/{expense.pk}/change/")
        self.assertContains(page, f"/expenses/{expense.pk}/")
        self.assertEqual(self.client.get("/admin/expenses/expense/add/").status_code, 403)


class ReceiptCameraTests(TestCase):
    def test_the_receipt_box_offers_the_rear_camera(self):
        self.client.force_login(make_member())
        page = self.client.get("/expenses/claim/")
        self.assertContains(page, 'data-camera="environment"')
        self.assertContains(page, "js/camera")
