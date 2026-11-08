from django.core.management.base import BaseCommand
from django.db import transaction

from apps.billing import emails
from apps.proposals import voting


class Command(BaseCommand):
    help = (
        "Open proposals whose voting has started (emails voters), count those whose voting has closed, and announce "
        "both on Discord. Runs hourly (spec 06 §6.3)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report only: nothing is saved, emailed or posted.")

    def handle(self, *args, dry_run=False, **options):
        if dry_run:
            with emails.suppressed(), transaction.atomic():
                results = voting.update_all()
                transaction.set_rollback(True)
        else:
            results = voting.update_all()
        prefix = "Dry run: would have " if dry_run else ""
        self.stdout.write(f"{prefix}opened {results['opened']}, counted {results['counted']} proposal(s).")
