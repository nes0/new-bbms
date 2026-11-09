"""Record how many members have each status today, for the membership statistics (spec 06 §6.13). Run daily, late
in the evening; running it again the same day updates the day's figures."""

from django.core.management.base import BaseCommand

from apps.members import statistics


class Command(BaseCommand):
    help = "Record today's membership totals for the statistics page."

    def handle(self, *args, **options):
        snapshot = statistics.take_snapshot()
        self.stdout.write(f"Members on {snapshot.date}: {snapshot.counts}")
