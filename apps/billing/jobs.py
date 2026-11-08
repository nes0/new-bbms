"""
Shared plumbing for the scheduled billing jobs (spec 03 §3.6).

Each job takes `--date` (run as if it were that day: for catching up a missed night, or testing) and `--dry-run`
(work out what would happen, report it, then undo it all: nothing is sent to GoCardless and nobody is emailed). The
shadow site runs every job with `--dry-run` (08).

A normal run (no `--date`, no `--dry-run`) pings the job's heartbeat (`heartbeat.py`), so a missed or failed night
alerts someone. Runs by hand with `--date` or `--dry-run` don't ping: they aren't the scheduled run.
"""

from contextlib import contextmanager
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from . import emails, gocardless, heartbeat


@contextmanager
def dry_run(enabled=True):
    if not enabled:
        yield None
        return
    with transaction.atomic():
        with gocardless.use(gocardless.DryRunGoCardless()) as fake, emails.suppressed():
            yield fake
        transaction.set_rollback(True)


class JobCommand(BaseCommand):
    """A billing job: subclasses define `run(today)`, returning a dict of counts to report."""

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Run as if today were this date (YYYY-MM-DD).")
        parser.add_argument("--dry-run", action="store_true", help="Report what would happen; change nothing.")

    @property
    def job(self):
        """The command's name, e.g. "bill_members"."""
        return type(self).__module__.rsplit(".", 1)[-1]

    def handle(self, *args, **options):
        today = timezone.localdate()
        if options["date"]:
            try:
                today = date.fromisoformat(options["date"])
            except ValueError as error:
                raise CommandError("--date must be YYYY-MM-DD") from error
        scheduled = not options["date"] and not options["dry_run"]
        if scheduled:
            heartbeat.ping(self.job, "start")
        try:
            with dry_run(options["dry_run"]) as fake:
                results = self.run(today)
        except Exception as error:
            if scheduled:
                heartbeat.ping(self.job, "fail", f"{type(error).__name__}; see the job's log")
            raise
        prefix = "Dry run (nothing changed): " if options["dry_run"] else ""
        summary = ", ".join(f"{key} {value}" for key, value in results.items()) or "nothing to do"
        line = f"{prefix}{self.help.split('.')[0]} for {today}: {summary}"
        self.stdout.write(line)
        if scheduled:
            heartbeat.ping(self.job, message=line)
        if fake is not None and fake.calls:
            self.stdout.write(f"Would have sent GoCardless {len(fake.calls)} request(s).")

    def run(self, today):
        raise NotImplementedError
