"""
Copy the legacy system's data into this (empty) database. See apps/legacy/importer.py and spec 08 §8.2.

    LEGACY_DATABASE_URL=mysql://user:password@127.0.0.1:3306/bbms python manage.py import_legacy
    python manage.py import_legacy --dry-run      # do everything, print the report, then undo it

Options for cutover (spec 08 §8.2):
    --mark-inducted     everyone who has been a member counts as having had their general induction confirmed (05
                        §5.2: the door needs it, and legacy didn't check it). Logged in each member's audit log.
    --photos-from URL   copy members' and equipment photos from legacy's S3 folder, e.g.
                        https://s3-eu-west-1.amazonaws.com/<bucket>/production (the photos are public there). Needs
                        internet access; in a dry run they're only checked, not copied.

Give it a read-only MySQL user. The report shows only legacy ids and counts, so it's safe to share.
"""

import os
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.access.models import Device, KeyFob
from apps.equipment.models import Equipment
from apps.legacy.importer import Importer
from apps.legacy.photos import PhotoSource
from apps.legacy.source import MySQLSource
from apps.members.models import Member


class Command(BaseCommand):
    help = (
        "Import members, addresses, photos, payments, charges, fobs, devices, equipment, inductions, sessions and the "
        "door log from the legacy MySQL."
    )

    def add_arguments(self, parser):
        parser.add_argument("--source", help="Legacy database URL (default: the LEGACY_DATABASE_URL variable).")
        parser.add_argument("--dry-run", action="store_true", help="Import, report, then roll everything back.")
        parser.add_argument(
            "--mark-inducted",
            action="store_true",
            help="Give every imported member (except those still setting up) a confirmed general induction.",
        )
        parser.add_argument(
            "--photos-from",
            metavar="URL",
            help="Copy members' and equipment photos from this legacy S3 folder (https://.../<bucket>/production).",
        )

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

        photos = None
        if options["photos_from"]:
            try:
                photos = PhotoSource(options["photos_from"], save=not options["dry_run"])
            except ValueError as error:
                raise CommandError(str(error)) from error

        started = time.monotonic()
        with transaction.atomic():
            importer = Importer(MySQLSource(url), mark_inducted=options["mark_inducted"], photos=photos)
            report = importer.run()
            for line in report.lines():
                self.stdout.write(line)
            if options["dry_run"]:
                transaction.set_rollback(True)
        seconds = time.monotonic() - started
        outcome = "Dry run: nothing was kept." if options["dry_run"] else "Done."
        self.stdout.write(self.style.SUCCESS(f"{outcome} ({seconds:.1f} s)"))
