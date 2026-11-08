from datetime import timedelta

from django.utils import timezone

from apps.equipment.models import Induction
from apps.equipment.tests.helpers import laser, member
from apps.site_settings.models import SiteSettings

COURSE = 7


def course_machine(**fields):
    """A laser that needs online course 7."""
    defaults = {"moodle_course_id": COURSE, "moodle_course_since": timezone.now() - timedelta(days=7), **fields}
    return laser(**defaults)


def waiting(who, machine, paid=True):
    """Paid for training, not signed off yet."""
    return Induction.objects.create(member=who, category=machine.induction_key, paid=paid)


def set_moodle_address(url="https://moodle.example.com"):
    row = SiteSettings.load()
    row.moodle_url = url
    row.save()


__all__ = ["COURSE", "course_machine", "member", "set_moodle_address", "waiting"]
