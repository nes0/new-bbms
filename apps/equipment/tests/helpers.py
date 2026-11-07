from django.contrib.auth.models import Group
from django.utils import timezone

from apps.equipment.models import Equipment, Induction
from apps.members.models import Member

_count = 0


def member(**fields):
    global _count
    _count += 1
    defaults = dict(given_name="Mo", family_name=f"Member{_count}", status=Member.Status.ACTIVE)
    defaults.update(fields)
    return Member.objects.create_user(f"m{_count}@example.com", "a-good-password", **defaults)


def laser(**fields):
    defaults = dict(
        name="Red laser",
        slug="laser",
        requires_induction=True,
        induction_category="laser",
        usage_cost_pence_per_hour=300,
        team=Group.objects.get_or_create(name="laser")[0],
    )
    defaults.update(fields)
    return Equipment.objects.create(**defaults)


def trained(who, equipment, when=None):
    return Induction.objects.create(
        member=who, category=equipment.induction_key, paid=True, trained_at=when or timezone.now()
    )
