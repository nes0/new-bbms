"""
Copy the legacy system's data into this (empty) database. See apps/legacy/importer.py and spec 08 §8.2.

    LEGACY_DATABASE_URL=mysql://user:password@127.0.0.1:3306/bbms python manage.py import_legacy
    python manage.py import_legacy --dry-run      # do everything, print the report, then undo it

Give it a read-only MySQL user. The report shows only legacy ids and counts, so it's safe to share.
"""

import os
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.access.models import Device, KeyFob
from apps.equipment.models import Equipment
from apps.legacy.importer import Importer
from apps.legacy.source import MySQLSource
from apps.members.models import Member


class Command(BaseCommand):
    help = "Import members, fobs, devices, equipment, inductions, sessions and the door log from the legacy MySQL."

    def add_arguments(self, parser):
        parser.add_argument("--source", help="Legacy database URL (default: the LEGACY_DATABASE_URL variable).")
        parser.add_argument("--dry-run", action="store_true", help="Import, report, then roll everything back.")

    def handle(self, *args, **options):
        url = options["source"] or os.environ.get("LEGACY_DATABASE_URL")
        if not url:
            raise CommandError("Give the legacy database with --source or LEGACY_DATABASE_URL.")
        for model in (Member, KeyFob, Device, Equipment):
            if model.objects.exists():
                raise CommandError(
                    f"This database already has {model._meta.verbose_name_plural}. The import only runs into an "
                    "empty database: start from a fresh one (or `manage.py flush`)."
                )

        started = time.monotonic()
        with transaction.atomic():
            report = Importer(MySQLSource(url)).run()
            for line in report.lines():
                self.stdout.write(line)
            if options["dry_run"]:
                transaction.set_rollback(True)
        seconds = time.monotonic() - started
        outcome = "Dry run: nothing was kept." if options["dry_run"] else "Done."
        self.stdout.write(self.style.SUCCESS(f"{outcome} ({seconds:.1f} s)"))
