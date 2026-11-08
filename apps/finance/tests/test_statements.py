"""Bank statement import (spec 04 §4.6, §4.9). All names, references and amounts here are made up."""

from datetime import timedelta

from django.contrib.auth.models import Group, Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import Charge
from apps.equipment.tests.helpers import member
from apps.finance import statements
from apps.finance.models import BankLine, BankMatch, BankStatement
from apps.members.models import Member
from apps.payments.credit import credit_pence
from apps.payments.models import Payment

DAY = timezone.localdate() - timedelta(days=10)
UK = f"{DAY:%d/%m/%Y}"


def csv_file(*lines, name="statement.csv"):
    return SimpleUploadedFile(name, ("\n".join(lines) + "\n").encode(), content_type="text/csv")


def legacy_layout(*rows):
    """Legacy's layout: date, type, description, (unused), amount."""
    return csv_file(*(f"{UK},{kind},{description},,{amount}" for kind, description, amount in rows))


def finance_member(**fields):
    who = member(given_name="Fin", family_name="Ance", **fields)
    group, _ = Group.objects.get_or_create(name="finance")
    group.permissions.add(
        *Permission.objects.filter(codename__in=("see_finance", "manage_finance", "see_all_expenses"))
    )
    group.user_set.add(who)
    return who


class ReadingTests(TestCase):
    def test_legacy_layout_keeps_credits_only(self):
        upload = legacy_layout(("CR", "K LEE SUB", "20.00"), ("DR", "ELECTRICITY", "120.00"), ("CR", "X", '"1,234.50"'))
        credits, skipped = statements.read_credits(upload)
        self.assertEqual(
            [(c.description, c.amount_pence, c.date) for c in credits], [("K LEE SUB", 2000, DAY), ("X", 123450, DAY)]
        )
        self.assertEqual(skipped, 1)

    def test_a_header_row_is_read_by_name(self):
        upload = csv_file(
            "Transaction Date,Transaction Type,Sort Code,Account Number,Transaction Description,Debit Amount,"
            "Credit Amount,Balance",
            f"{UK},FPI,'00-00-00,00000000,K LEE SUB,,15.00,100.00",
            f"{UK},DD,'00-00-00,00000000,WATER,30.00,,85.00",
            "Opening balance,,,,,,,",
        )
        credits, skipped = statements.read_credits(upload)
        self.assertEqual([(c.description, c.amount_pence) for c in credits], [("K LEE SUB", 1500)])
        self.assertEqual(skipped, 2)
        signed = csv_file("Date,Description,Amount", f"{DAY:%Y-%m-%d},K LEE SUB,15", f"{DAY:%Y-%m-%d},RENT,-500")
        self.assertEqual([c.amount_pence for c in statements.read_credits(signed)[0]], [1500])

    def test_bad_files(self):
        for upload, message in (
            (csv_file(""), "empty"),
            (csv_file("Date,Description", f"{UK},X"), "paid-in"),
            (legacy_layout(("DR", "RENT", "500")), "No money coming in"),
            (csv_file(f"01/01/{timezone.localdate().year + 2},CR,X,,5"), "in the future"),
            (SimpleUploadedFile("s.csv", b"\xff\xd8\x00\x00binary"), "CSV"),
        ):
            with self.assertRaisesMessage(statements.BadStatement, message):
                statements.read_credits(upload)

    def test_sorting_lines(self):
        self.assertEqual(statements.kind_of("GC C1 BUILDBRIGHTO ABC"), "gocardless")
        self.assertEqual(statements.kind_of("Stripe Transfer 123"), "stripe")
        self.assertEqual(statements.kind_of("EVENTBRITE"), "other")
        self.assertEqual(statements.kind_of("K LEE SUB"), "member")
        self.assertEqual(statements.reason_of("K LEE MEMBERSHIP"), ("subscription", ""))
        self.assertEqual(statements.reason_of("K LEE-BALANCE-AB12"), ("balance", "ab12"))
        self.assertEqual(statements.reason_of("K LEE"), ("", ""))


class MatchingTests(TestCase):
    def match(self, description):
        return statements.Matcher().match(description)

    def test_family_name_as_a_whole_word(self):
        kim = member(given_name="Kim", family_name="Lee")
        self.assertEqual(self.match("K LEE SUB").member, kim)
        self.assertIsNone(self.match("FLEET STREET SUB").member)  # legacy matched "LEE" in "FLEET"
        member(given_name="Gone", family_name="Fleet", status=Member.Status.LEFT)
        self.assertIsNone(self.match("FLEET STREET SUB").member)  # not those who have left

    def test_shared_names_narrowed_by_first_name_or_initial(self):
        kim = member(given_name="Kim", family_name="Lee")
        sam = member(given_name="Sam", family_name="Lee")
        self.assertEqual(self.match("SAM LEE SUB").member, sam)
        self.assertEqual(self.match("LEE K SUB").member, kim)
        found = self.match("MR LEE SUB")
        self.assertIsNone(found.member)
        self.assertEqual(set(found.candidates), {kim, sam})

    def test_remembered_and_given_text_come_first(self):
        kim, sam = member(given_name="Kim", family_name="Lee"), member(given_name="Sam", family_name="Ray")
        BankMatch.objects.create(text="K LEE SUB", whole=True, member=sam)  # e.g. Sam pays for Kim
        self.assertEqual(self.match("k  lee sub").member, sam)
        BankMatch.objects.create(text="REF-777", whole=False, member=kim)
        self.assertEqual(self.match("PAYMENT REF-777 RAY").member, kim)
        BankMatch.objects.create(text="RAY", whole=False, member=sam)
        self.assertIsNone(self.match("PAYMENT REF-777 RAY").member)  # two pieces, two members: ask


class ImportTests(TestCase):
    def setUp(self):
        self.fin = finance_member()
        self.client.force_login(self.fin)
        self.kim = member(
            given_name="Kim",
            family_name="Lee",
            payment_method=Member.PaymentMethod.STANDING_ORDER,
            monthly_subscription_pence=1500,
        )
        self.charge = Charge.objects.create(
            member=self.kim, charge_date=DAY - timedelta(days=3), status=Charge.Status.DUE
        )
        self.sam = member(given_name="Sam", family_name="Ray")

    def upload(self, upload):
        response = self.client.post("/finance/statements/", {"statement": upload})
        statement = BankStatement.objects.latest("pk")
        self.assertRedirects(response, f"/finance/statements/{statement.pk}/")
        return statement

    def statement_file(self):
        return legacy_layout(
            ("CR", "K LEE SUB", "20.00"),
            ("CR", "S RAY-BALANCE-AB12", "10.00"),
            ("CR", "MYSTERY PERSON", "7.00"),
            ("CR", "GC C1 BUILDBRIGHTO PAYOUT", "300.00"),
            ("CR", "EVENTBRITE", "50.00"),
            ("DR", "ELECTRICITY", "99.00"),
        )

    def fields(self, statement, overrides=None):
        """The review form as BBMS fills it in, with some lines changed: {row: (action, member text)}."""
        page = self.client.get(f"/finance/statements/{statement.pk}/")
        data = {"do": "record"}
        for row in page.context["rows"]:
            line = row["line"]
            if row["duplicate"]:
                continue
            action, text = (overrides or {}).get(line.row, (row["action"], row["member_text"]))
            data[f"line-{line.pk}-action"], data[f"line-{line.pk}-member"] = action, text
        return data

    def test_review_then_record(self):
        statement = self.upload(self.statement_file())
        page = self.client.get(f"/finance/statements/{statement.pk}/")
        self.assertContains(page, "Nothing has been recorded yet")
        self.assertContains(page, "Kim Lee (#")
        self.assertContains(page, "No match")
        self.assertFalse(Payment.objects.exists())  # a draft records nothing (legacy's "test mode")
        rows = {row["line"].row: row["action"] for row in page.context["rows"]}
        self.assertEqual(rows, {1: "subscription", 2: "balance", 3: "unmatched", 4: "payout", 5: "not-member"})

        response = self.client.post(f"/finance/statements/{statement.pk}/", self.fields(statement), follow=True)
        self.assertContains(response, "Recorded 2 payments")
        # Kim's standing order paid the waiting charge, on the day it reached the bank.
        self.charge.refresh_from_db()
        self.kim.refresh_from_db()
        self.assertEqual((self.charge.status, self.charge.paid_on, self.charge.amount_pence), ("paid", DAY, 2000))
        self.assertEqual(self.kim.monthly_subscription_pence, 2000)  # standing order amount follows what they pay
        self.assertEqual(self.kim.subscription_expires.month, (DAY.month % 12) + 1)
        sub = Payment.objects.get(member=self.kim)
        self.assertEqual(
            (sub.reason, sub.source, sub.charge, timezone.localtime(sub.created_at).date()),
            ("subscription", "standing-order", self.charge, DAY),
        )
        top_up = Payment.objects.get(member=self.sam)
        self.assertEqual((top_up.reason, top_up.reference, credit_pence(self.sam)), ("balance", "ab12", 1000))
        outcomes = dict(BankLine.objects.values_list("row", "outcome"))
        self.assertEqual(outcomes, {1: "recorded", 2: "recorded", 3: "unmatched", 4: "payout", 5: "not-member"})
        self.assertTrue(BankMatch.objects.filter(text="K LEE SUB", member=self.kim, whole=True).exists())
        # Recorded is recorded.
        self.client.post(f"/finance/statements/{statement.pk}/", self.fields(statement))
        self.assertEqual(Payment.objects.count(), 2)

    def test_the_same_lines_again_are_left_out(self):
        first = self.upload(self.statement_file())
        self.client.post(f"/finance/statements/{first.pk}/", self.fields(first))
        again = self.upload(self.statement_file())
        page = self.client.get(f"/finance/statements/{again.pk}/")
        self.assertContains(page, "Already recorded from an earlier statement", count=5)
        self.client.post(f"/finance/statements/{again.pk}/", self.fields(again))
        self.assertEqual(Payment.objects.count(), 2)
        # Two equal payments on one day are two lines, though.
        twice = self.upload(legacy_layout(("CR", "S RAY TOPUP", "5"), ("CR", "S RAY TOPUP", "5")))
        self.client.post(f"/finance/statements/{twice.pk}/", self.fields(twice))
        self.assertEqual(credit_pence(self.sam), 2000)

    def test_finance_chooses_and_corrects(self):
        statement = self.upload(self.statement_file())
        data = self.fields(
            statement, {3: ("subscription", f"Sam Ray (#{self.sam.pk})"), 1: ("balance", f"Kim Lee (#{self.kim.pk})")}
        )
        self.client.post(f"/finance/statements/{statement.pk}/", data)
        self.charge.refresh_from_db()
        self.assertEqual(self.charge.status, "due")  # recorded as a top-up instead
        self.assertEqual(credit_pence(self.kim), 2000)
        line = BankLine.objects.get(row=3)
        self.assertEqual((line.outcome, line.member), ("recorded", self.sam))
        self.assertIn("No subscription was waiting", line.note)  # Sam has no charge: so to credit
        self.assertEqual(credit_pence(self.sam), 1700)
        self.assertEqual(BankMatch.objects.get(text="MYSTERY PERSON").member, self.sam)

    def test_nothing_is_recorded_if_a_line_is_wrong(self):
        statement = self.upload(self.statement_file())
        data = self.fields(statement, {3: ("subscription", "Nobody we know")})
        response = self.client.post(f"/finance/statements/{statement.pk}/", data)
        self.assertContains(response, "Row 3: we don&#x27;t know that member.")
        self.assertContains(response, 'value="Nobody we know"')  # what they typed is kept
        self.assertFalse(Payment.objects.exists())
        data = self.fields(statement, {1: ("payout", f"Kim Lee (#{self.kim.pk})")})
        response = self.client.post(f"/finance/statements/{statement.pk}/", data)
        self.assertContains(response, "Only a GoCardless, Stripe or PayPal line is a payout")
        self.assertFalse(Payment.objects.exists())
        self.assertTrue(BankStatement.objects.get().is_draft)

    def test_throw_away_a_draft(self):
        statement = self.upload(self.statement_file())
        self.client.post(f"/finance/statements/{statement.pk}/", {"do": "discard"})
        self.assertFalse(BankStatement.objects.exists())
        self.assertFalse(BankLine.objects.exists())

    def test_matching_a_line_later(self):
        statement = self.upload(self.statement_file())
        self.client.post(f"/finance/statements/{statement.pk}/", self.fields(statement))
        line = BankLine.objects.get(outcome="unmatched")
        self.assertContains(self.client.get("/finance/"), "1 bank line not matched yet")
        self.client.post(f"/finance/bank/{line.pk}/", {"action": "balance", "member": str(self.sam.pk)})
        line.refresh_from_db()
        self.assertEqual((line.outcome, line.payment.member), ("recorded", self.sam))
        self.assertEqual(credit_pence(self.sam), 1700)
        response = self.client.post(f"/finance/bank/{line.pk}/", {"action": "balance", "member": str(self.sam.pk)})
        self.assertEqual(credit_pence(self.sam), 1700)  # only once
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(self.client.get("/finance/bank/?outcome=unmatched"), "MYSTERY PERSON")

    def test_bad_upload_says_why(self):
        response = self.client.post("/finance/statements/", {"statement": csv_file("nothing here")}, follow=True)
        self.assertContains(response, "columns for date, type, description and amount")
        self.assertFalse(BankStatement.objects.exists())

    def test_spreadsheet_text_is_shown_as_text(self):
        statement = self.upload(legacy_layout(("CR", "<script>alert(1)</script>", "5")))
        page = self.client.get(f"/finance/statements/{statement.pk}/")
        self.assertNotContains(page, "<script>alert")
        self.assertContains(page, "&lt;script&gt;")


class WhoCanTests(TestCase):
    def test_finance_role_and_admins_only(self):
        self.client.force_login(member())
        for url in ("/finance/", "/finance/payments/", "/finance/charges/", "/finance/statements/", "/finance/bank/"):
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertNotContains(self.client.get("/"), "/finance/")
        admin = Member.objects.create_superuser("a@example.com", "a-good-password", given_name="A", family_name="D")
        self.client.force_login(admin)
        self.assertContains(self.client.get("/"), 'href="/finance/"')
        self.assertEqual(self.client.get("/finance/statements/").status_code, 200)

    def test_see_only(self):
        viewer = member()
        viewer.user_permissions.add(Permission.objects.get(codename="see_finance"))
        self.client.force_login(viewer)
        self.assertNotContains(self.client.get("/finance/statements/"), "Import a statement")
        upload = legacy_layout(("CR", "X", "5"))
        self.assertEqual(self.client.post("/finance/statements/", {"statement": upload}).status_code, 403)
