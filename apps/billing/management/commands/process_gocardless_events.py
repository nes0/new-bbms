from django.core.management.base import BaseCommand

from apps.billing.webhooks import process_pending


class Command(BaseCommand):
    help = "Handle GoCardless webhook events that are waiting (or, with --retry-failed, ones that failed)."

    def add_arguments(self, parser):
        parser.add_argument("--retry-failed", action="store_true")

    def handle(self, *args, **options):
        count = process_pending(retry_failed=options["retry_failed"])
        self.stdout.write(f"Handled {count} GoCardless event(s).")
