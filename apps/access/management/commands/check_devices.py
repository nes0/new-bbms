from django.core.management.base import BaseCommand

from apps.access.monitoring import check_devices


class Command(BaseCommand):
    help = "Tell the acs team about monitored devices that have gone quiet. Runs every 15 minutes (spec 05 §5.5)."

    def handle(self, *args, **options):
        self.stdout.write(f"Told {check_devices()} people about quiet devices.")
