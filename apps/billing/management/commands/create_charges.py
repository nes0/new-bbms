from apps.billing.billing import create_charges
from apps.billing.jobs import JobCommand


class Command(JobCommand):
    help = "Create subscription and locker rent charges up to a week ahead. Runs at 01:00 (spec 03 §3.6)."

    def run(self, today):
        return create_charges(today)
