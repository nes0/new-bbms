"""Hourly: close stale sessions, merge back-to-back ones, drop very short ones (spec 05 §5.6).

Run by an App Platform scheduled job:  python manage.py tidy_equipment_sessions
Safe to run any number of times.
"""

from django.core.management.base import BaseCommand

from apps.equipment import rules


class Command(BaseCommand):
    help = "Close stale equipment sessions and merge back-to-back ones."

    def handle(self, *args, **options):
        counts = rules.tidy_up()
        self.stdout.write(f"Closed {counts['closed']} stale session(s); merged {counts['merged']}.")
