"""GoCardless and Stripe payouts: real fees, matching bank lines, and money received (spec 04 §4.9). All names,
amounts and ids are made up."""

from datetime import timedelta
from io import StringIO
from types import SimpleNamespace

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.billing import gocardless
from apps.equipment.tests.helpers import member
from apps.finance import money, payouts, statements
from apps.finance.models import BankLine, BankStatement, Payout, PayoutItem
from apps.finance.tests.test_statements import finance_member, legacy_layout
from apps.payments import cards, services
from apps.payments.models import Payment

TODAY = timezone.localdate()
ARRIVED = TODAY - timedelta(days=5)
SUB, GC, STRIPE = Payment.Reason.SUBSCRIPTION, Payment.Source.GOCARDLESS, Payment.Source.STRIPE


def bank_line(kind, amount, day=ARRIVED, description="GC C1 BUILDBRIGHTO", outcome=BankLine.Outcome.PAYOUT):
    statement, _ = BankStatement.objects.get_or_create(filename="test.csv", defaults={"recorded_at": timezone.now()})
    return BankLine.objects.create(
        statement=statement,
        row=BankLine.objects.count() + 1,
        date=day,
        description=description,
        amount_pence=amount,
        kind=kind,
        fingerprint=f"{day}{amount}{description}{BankLine.objects.count()}",
        kept=True,
        outcome=outcome,
    )


class PayoutsTestCase(TestCase):
    def setUp(self):
        self.gc = gocardless.reset_fake()
        self.stripe = cards.reset_fake()
        self.kim = member(given_name="Kim", family_name="Lee")
        self.sam = member(given_name="Sam", family_name="Ray")
        self.dd = services.record_payment(self.kim, SUB, GC, 2500, source_id="PM000101")
        self.card = services.record_payment(self.sam, SUB, STRIPE, 3000, source_id="pi_test_1", fee_pence=65)


class GoCardlessItemsTests(TestCase):
    def test_a_payment_and_its_fee_are_one_item_and_refunds_are_listed(self):
        payout = gocardless.payout_dict(
            "PO1",
            2245,
            70,
            "2026-11-03",
            "paid",
            "REF-1",
            [
                ("payment_paid_out", "2500.0", "PM1"),
                ("gocardless_fee", "-45.4", "PM1"),
                ("app_fee", "-25.0", "PM1"),
                ("payment_charged_back", "-180.0", "PM9"),
            ],
        )
        self.assertEqual(payout["arrival_date"].isoformat(), "2026-11-03")
        first, second = payout["items"]
        self.assertEqual((first["kind"], first["amount_pence"], first["fee_pence"]), ("payment", 2500, 70))
        self.assertEqual((second["kind"], second["amount_pence"]), ("refund", -180))


class StripeItemsTests(TestCase):
    def test_a_card_payment_is_found_by_its_payment_intent(self):
        charge = SimpleNamespace(
            type="charge", amount=3000, fee=65, description=None, source=SimpleNamespace(payment_intent="pi_9")
        )
        refund = SimpleNamespace(type="refund", amount=-1000, fee=0, description="Refund", source="re_1")
        self.assertEqual(cards.stripe_item(charge)["payment_id"], "pi_9")
        self.assertEqual(cards.stripe_item(refund)["kind"], "refund")


class ImportTests(PayoutsTestCase):
    def test_payouts_are_stored_with_real_fees_on_the_payments(self):
        self.gc.add_payout(
            ARRIVED,
            [
                ("payment_paid_out", "2500.0", "PM000101"),
                ("gocardless_fee", "-45.0", "PM000101"),
                ("payment_paid_out", "1000.0", "PM_UNKNOWN"),
            ],
            reference="BB-0001",
        )
        self.stripe.add_payout(ARRIVED, [(3000, 62, "pi_test_1")])
        summary = payouts.import_payouts()
        self.assertEqual((summary.new, summary.fees_set, summary.unknown_payments), (2, 2, 1))
        self.dd.refresh_from_db()
        self.card.refresh_from_db()
        self.assertEqual((self.dd.fee_pence, self.card.fee_pence), (45, 62))  # was £0 and Stripe's first figure
        self.assertEqual(self.dd.amount_pence, 2500)  # the money itself is never touched
        gc_payout = Payout.objects.get(provider="gocardless")
        self.assertEqual((gc_payout.amount_pence, gc_payout.fees_pence, gc_payout.reference), (3455, 45, "BB-0001"))
        self.assertEqual(PayoutItem.objects.get(provider_payment_id="PM000101").payment, self.dd)

    def test_importing_again_changes_nothing(self):
        self.gc.add_payout(
            ARRIVED, [("payment_paid_out", "2500.0", "PM000101"), ("gocardless_fee", "-45.0", "PM000101")]
        )
        payouts.import_payouts()
        summary = payouts.import_payouts()
        self.assertEqual((summary.new, summary.updated, summary.fees_set), (0, 0, 0))
        self.assertEqual((Payout.objects.count(), PayoutItem.objects.count()), (1, 1))

    def test_a_status_change_is_picked_up(self):
        self.gc.add_payout(ARRIVED, [("payment_paid_out", "2500.0", "PM000101")], status="pending")
        payouts.import_payouts()
        self.gc.payouts_made[0]["status"] = "paid"
        self.assertEqual(payouts.import_payouts().updated, 1)
        self.assertEqual(Payout.objects.get().status, "paid")

    def test_one_provider_failing_doesnt_stop_the_other(self):
        self.stripe.add_payout(ARRIVED, [(3000, 62, "pi_test_1")])
        self.gc.fail_next = "GoCardless couldn't list payouts: down"
        summary = payouts.import_payouts()
        self.assertEqual(summary.errors, ["GoCardless: GoCardless couldn't list payouts: down"])
        self.assertTrue(Payout.objects.filter(provider="stripe").exists())

    def test_the_nightly_command(self):
        self.gc.add_payout(ARRIVED, [("payment_paid_out", "2500.0", "PM000101")])
        out = StringIO()
        call_command("import_payouts", stdout=out)
        self.assertIn("Payouts: 1 new payout(s)", out.getvalue())

    def test_the_first_import_looks_back_and_later_ones_overlap(self):
        self.assertEqual(payouts.default_since("gocardless"), TODAY - timedelta(days=120))
        Payout.objects.create(
            provider="gocardless", provider_id="PO9", amount_pence=1, arrival_date=ARRIVED, status="paid"
        )
        self.assertEqual(payouts.default_since("gocardless"), ARRIVED - timedelta(days=14))


class MatchingTests(PayoutsTestCase):
    def payout(self, amount, day=ARRIVED, reference="", provider="gocardless"):
        return Payout.objects.create(
            provider=provider,
            provider_id=f"PO{Payout.objects.count()}",
            amount_pence=amount,
            arrival_date=day,
            status="paid",
            reference=reference,
        )

    def test_a_bank_line_is_matched_to_the_payout_of_the_same_amount_near_its_date(self):
        payout = self.payout(3455, day=ARRIVED - timedelta(days=2))
        self.payout(9999)
        line = bank_line(BankLine.Kind.GOCARDLESS, 3455)
        self.assertEqual(payouts.match_bank_lines(), 1)
        payout.refresh_from_db()
        self.assertEqual(payout.bank_line, line)

    def test_too_far_apart_or_the_wrong_provider_isnt_matched(self):
        self.payout(3455, day=ARRIVED - timedelta(days=10))
        self.payout(3455, provider="stripe")
        bank_line(BankLine.Kind.GOCARDLESS, 3455)
        self.assertEqual(payouts.match_bank_lines(), 0)

    def test_the_reference_on_the_statement_decides_between_two(self):
        self.payout(3455, reference="BB-AAA")
        wanted = self.payout(3455, day=ARRIVED + timedelta(days=1), reference="BB-BBB")
        line = bank_line(BankLine.Kind.GOCARDLESS, 3455, description="GOCARDLESS BB-BBB")
        payouts.match_bank_lines()
        wanted.refresh_from_db()
        self.assertEqual(wanted.bank_line, line)

    def test_recording_a_statement_matches_payouts_already_imported(self):
        payout = self.payout(3455, day=TODAY - timedelta(days=10))
        upload = legacy_layout(("CR", "GC C1 BUILDBRIGHTO", "34.55"))
        fin = finance_member()
        statement = statements.read_statement(upload, fin)
        line = statement.lines.get()
        counts = statements.record(statement, {line.pk: (statements.PAYOUT, None)}, fin)
        self.assertEqual(counts["payouts matched"], 1)
        payout.refresh_from_db()
        self.assertEqual(payout.bank_line, line)


class ReceivedTests(PayoutsTestCase):
    def test_bbms_and_the_bank_side_by_side_with_what_to_look_at(self):
        Payout.objects.create(
            provider="gocardless", provider_id="PO1", amount_pence=3455, arrival_date=ARRIVED, status="paid"
        )
        Payout.objects.create(
            provider="stripe", provider_id="po_1", amount_pence=2938, arrival_date=ARRIVED, status="paid"
        )
        bank_line(BankLine.Kind.GOCARDLESS, 3455)
        payouts.match_bank_lines()
        bank_line(BankLine.Kind.MEMBER, 1500, description="K LEE", outcome=BankLine.Outcome.UNMATCHED)
        report = money.received(ARRIVED.replace(day=1), TODAY)
        rows = {row["key"]: row for row in report["rows"]}
        self.assertEqual(
            (rows["gocardless"]["bbms"], rows["gocardless"]["bank"], rows["gocardless"]["difference"]), (3455, 3455, 0)
        )
        self.assertEqual((rows["stripe"]["bbms"], rows["stripe"]["bank"]), (2938, 0))
        self.assertEqual(rows["member"]["difference"], 1500)
        self.assertEqual([line.description for line in report["unmatched_lines"]], ["K LEE"])
        self.assertEqual([p.provider for p in report["payouts_not_on_statement"]], ["stripe"])


class PageTests(PayoutsTestCase):
    def setUp(self):
        super().setUp()
        self.fin = finance_member()
        self.client.force_login(self.fin)

    def test_finance_imports_and_reads_payouts(self):
        self.gc.add_payout(
            ARRIVED, [("payment_paid_out", "2500.0", "PM000101"), ("gocardless_fee", "-45.0", "PM000101")]
        )
        response = self.client.post("/finance/payouts/", follow=True)
        self.assertContains(response, "Payouts imported: 1 new payout(s)")
        self.assertContains(response, "£24.55")
        payout = Payout.objects.get()
        page = self.client.get(f"/finance/payouts/{payout.pk}/")
        self.assertContains(page, "Kim Lee")
        self.assertContains(page, "£0.45")

    def test_money_received_page(self):
        month = ARRIVED.strftime("%Y-%m")
        page = self.client.get(f"/finance/received/?month={month}")
        self.assertContains(page, "GoCardless payouts")
        self.assertContains(page, "No bank statement covering this period")

    def test_only_finance(self):
        self.client.force_login(member())
        for url in ("/finance/payouts/", "/finance/received/"):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post("/finance/payouts/").status_code, 403)
