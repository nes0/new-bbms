from apps.billing.billing import retry_direct_debits
from apps.billing.jobs import JobCommand


class Command(JobCommand):
    help = "Ask GoCardless to retry failed Direct Debits whose retry day has come. Runs daily (spec 03 §3.6)."

    def run(self, today):
        return {"retried": retry_direct_debits(today)}
