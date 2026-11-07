"""Daily: delete proofs of identity that nobody has checked in time (spec 03 §3.1, "Proof of identity").

Run by an App Platform scheduled job:  python manage.py shred_stale_id_documents
Safe to run any number of times.
"""

from django.core.management.base import BaseCommand

from apps.members import id_documents


class Command(BaseCommand):
    help = "Delete uploaded proofs of identity older than the time limit that haven't been checked."

    def handle(self, *args, **options):
        deleted = id_documents.shred_stale()
        self.stdout.write(f"Deleted {deleted} unchecked ID document(s) older than {id_documents.MAX_AGE.days} days.")
