from apps.billing.billing import bill_members
from apps.billing.jobs import JobCommand


class Command(JobCommand):
    help = "Bill due charges, from credit or by Direct Debit. Runs at 01:30 (spec 03 §3.6)."

    def run(self, today):
        return bill_members(today)
