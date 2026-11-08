from datetime import date

from django.test import TestCase, override_settings

from apps.billing import gocardless
from apps.equipment.tests.helpers import member
from apps.members.models import Member
from apps.payments import services
from apps.payments.models import Payment
from apps.storage import rules as storage
from apps.storage.models import Locker, LockerRent, LockerSize, StorageLocation

TODAY = date(2026, 12, 10)
WEBHOOK_SECRET = "test-webhook-secret"


def payer(method=Member.PaymentMethod.DIRECT_DEBIT, *, day=TODAY.day, amount=2500, **fields):
    defaults = dict(
        status=Member.Status.ACTIVE,
        payment_method=method,
        payment_day=day,
        monthly_subscription_pence=amount,
        mandate_id="MD-TEST1" if method == Member.PaymentMethod.DIRECT_DEBIT else "",
    )
    defaults.update(fields)
    return member(**defaults)


def top_up(who, pence):
    return services.record_payment(who, Payment.Reason.BALANCE, Payment.Source.CASH, pence)


@override_settings(GOCARDLESS_ACCESS_TOKEN="", GOCARDLESS_WEBHOOK_SECRET=WEBHOOK_SECRET)
class BillingTestCase(TestCase):
    def setUp(self):
        self.gc = gocardless.reset_fake()

    def rent_locker(self, who, rent=800, override=None):
        location, _ = StorageLocation.objects.get_or_create(name="Workshop wall")
        size, _ = LockerSize.objects.get_or_create(name="Large")
        LockerRent.objects.get_or_create(location=location, size=size, defaults={"monthly_rent_pence": rent})
        number = Locker.objects.count() + 1
        locker = Locker.objects.create(location=location, number=number, size=size)
        rental = storage.start_rental(locker, who, None)
        if override is not None:
            storage.set_rental_rent(rental, override, "volunteer", None)
        rental.started_at = rental.started_at.replace(year=2026, month=1, day=1)
        rental.save(update_fields=["started_at"])
        return rental
