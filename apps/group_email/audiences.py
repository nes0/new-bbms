"""
Who a member may email, and who is in each group (spec 06 §6.6).

A group is named by a key:
- `members`, `trusted`, `key-holders`;
- `role:<role id>`;
- `trained:<machine>` and `waiting:<machine>`, where the machine is its induction category (machines sharing a
  category share one induction, so they share these groups too).

Everyone in a group is a current member (active, leaving or honorary), not banned, with a login that's turned on.
Admins (superusers) may email every group, since they decide the rules anyway. Everyone else may email the groups
their rules give them (`SenderRule`), and only while they're a current member themselves.
"""

from dataclasses import dataclass

from django.contrib.auth.models import Group
from django.db.models import QuerySet

from apps.equipment.models import Equipment, Induction
from apps.members.models import Member

from .models import SenderRule

Audience = SenderRule.Audience


@dataclass(frozen=True)
class Choice:
    """A group the sender may choose."""

    key: str
    label: str  # "members trained on the Laser cutter"


def current_members() -> QuerySet:
    return Member.objects.filter(status__in=Member.STATUSES_WITH_ACCESS, banned=False, is_active=True)


def machine_names() -> dict[str, str]:
    """Each induction category's name: its machine's name, or its machines' names ("Drill and Pillar drill")."""
    names: dict[str, list[str]] = {}
    for equipment in Equipment.objects.filter(requires_induction=True, archived=False).order_by("name"):
        names.setdefault(equipment.induction_key, []).append(equipment.name)
    return {key: _and(found) for key, found in names.items()}


def _and(words: list[str]) -> str:
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


def label(key: str, names: dict[str, str] | None = None) -> str:
    """How a group is described to the sender and in the email's footer."""
    kind, _, value = key.partition(":")
    if kind == Audience.MEMBERS:
        return "all current members"
    if kind == Audience.TRUSTED:
        return "trusted members"
    if kind == Audience.KEY_HOLDERS:
        return "key holders"
    if kind == Audience.ROLE:
        role = Group.objects.filter(pk=value).first() if value.isdigit() else None
        return f"members of the {role.name} role" if role else "a role"
    machine = (names if names is not None else machine_names()).get(value, value)
    if kind == Audience.TRAINED:
        return f"members trained on the {machine}"
    return f"members waiting for training on the {machine}"


def members_of(key: str) -> QuerySet:
    """Everyone in a group (an empty set for a key that means nothing)."""
    kind, _, value = key.partition(":")
    members = current_members()
    if kind == Audience.MEMBERS and not value:
        return members
    if kind == Audience.TRUSTED and not value:
        return members.filter(trusted=True)
    if kind == Audience.KEY_HOLDERS and not value:
        return members.filter(key_holder=True)
    if kind == Audience.ROLE and value.isdigit():
        return members.filter(groups__pk=int(value)).distinct()
    if kind in (Audience.TRAINED, Audience.WAITING) and value:
        # As the machine's page lists them (05 §5.7): paid, and signed off or not yet.
        inductions = Induction.objects.filter(category=value, paid=True, trained_at__isnull=kind == Audience.WAITING)
        return members.filter(pk__in=inductions.values("member_id"))
    return Member.objects.none()


def _machines_looked_after(roles) -> set[str]:
    """The machines (induction categories) these roles look after."""
    machines = Equipment.objects.filter(team__in=roles, requires_induction=True, archived=False)
    return {equipment.induction_key for equipment in machines.only("slug", "induction_category")}


def _machines_trained_on(member) -> set[str]:
    return set(
        Induction.objects.filter(member=member, is_trainer=True, paid=True, trained_at__isnull=False).values_list(
            "category", flat=True
        )
    )


def allowed(member) -> list[Choice]:
    """The groups this member may email, in the order they're offered."""
    if not member.is_authenticated or member.banned or not member.is_active:
        return []
    names = machine_names()
    keys: set[str] = set()
    if member.is_superuser:
        keys |= {Audience.MEMBERS.value, Audience.TRUSTED.value, Audience.KEY_HOLDERS.value}
        keys |= {f"{Audience.ROLE}:{pk}" for pk in Group.objects.values_list("pk", flat=True)}
        keys |= {f"{kind}:{machine}" for machine in names for kind in SenderRule.MACHINE_AUDIENCES}
    elif member.has_space_access:
        roles = list(member.groups.all())
        for rule in SenderRule.objects.all():
            keys |= _keys_for(rule, member, roles)
    return sorted((Choice(key, label(key, names)) for key in keys), key=_order)


def _keys_for(rule: SenderRule, member, roles) -> set[str]:
    if rule.sender == SenderRule.Sender.ROLE:
        if rule.sender_role_id not in {role.pk for role in roles}:
            return set()
        if rule.audience in SenderRule.MACHINE_AUDIENCES:
            machines = {rule.category} if rule.category else _machines_looked_after([rule.sender_role_id])
        else:
            machines = set()
    elif rule.sender == SenderRule.Sender.MACHINE_TEAM:
        machines = _machines_looked_after(roles)
    else:
        machines = _machines_trained_on(member)
    if rule.audience in SenderRule.MACHINE_AUDIENCES:
        return {f"{rule.audience}:{machine}" for machine in machines if machine}
    if rule.audience == Audience.ROLE:
        return {f"{Audience.ROLE}:{rule.audience_role_id}"}
    return {rule.audience}


ORDER = [Audience.MEMBERS, Audience.TRUSTED, Audience.KEY_HOLDERS, Audience.ROLE, Audience.TRAINED, Audience.WAITING]


def _order(group: Choice):
    return (ORDER.index(group.key.partition(":")[0]), group.label.lower())


def may_email(member, key: str) -> bool:
    return any(group.key == key for group in allowed(member))
