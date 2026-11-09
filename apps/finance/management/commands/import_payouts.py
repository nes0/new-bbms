"""Import GoCardless and Stripe payouts, with the payments and fees inside them (spec 04 §4.9). Nightly."""

from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.billing import gocardless
from apps.finance import payouts
from apps.finance.models import Payout
from apps.payments import cards


class Command(BaseCommand):
    help = "Import GoCardless and Stripe payouts with their real fees, and match them to bank statement lines."

    def add_arguments(self, parser):
        parser.add_argument(
            "--since",
            help="Payouts created on or after this date (YYYY-MM-DD). Default: from the "
            "newest payout already imported, less two weeks; the first time, 120 days back.",
        )
        parser.add_argument("--provider", choices=Payout.Provider.values, help="Only this provider.")
        parser.add_argument(
            "--fake-payouts",
            action="store_true",
            help="Development only (DEBUG, no real keys): first pay out everything the fake GoCardless and Stripe "
            "have taken, so there's something to import.",
        )

    def handle(self, *args, **options):
        since = None
        if options["since"]:
            try:
                since = date.fromisoformat(options["since"])
            except ValueError as error:
                raise CommandError("--since must be YYYY-MM-DD") from error
        providers = [options["provider"]] if options["provider"] else None
        if options["fake_payouts"]:
            if not settings.DEBUG or settings.GOCARDLESS_ACCESS_TOKEN or settings.STRIPE_SECRET_KEY:
                raise CommandError("--fake-payouts is only for development, with the fake GoCardless and Stripe.")
            for fake in (gocardless.fake(), cards.fake()):
                fake.pay_out(timezone.localdate())
        summary = payouts.import_payouts(since, providers)
        self.stdout.write(f"Payouts: {summary}.")
        for error in summary.errors:
            self.stderr.write(f"Couldn't import from {error}")
        if summary.errors and len(summary.errors) == len(providers or Payout.Provider.values):
            raise CommandError("No provider could be asked.")
