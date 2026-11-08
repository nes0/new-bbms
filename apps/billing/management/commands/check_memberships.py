from apps.billing.jobs import JobCommand
from apps.billing.membership import check_memberships


class Command(JobCommand):
    help = "Suspend members who haven't paid, and mark members as left. Runs at 06:00 (spec 03 §3.6)."

    def run(self, today):
        return check_memberships(today)
