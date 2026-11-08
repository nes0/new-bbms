"""
The general induction (spec 05 §5.9, 03 §3.1).

Every new member reads the member agreement and the rules online and says they've done so; then someone with the
permission "Can confirm members' general inductions" (the `comms` role; admins have it) confirms it, usually while
the member is with them in the space. Both steps are logged in the members' audit log.
"""

from django.db import transaction
from django.utils import timezone

from . import changes
from .models import Member


class InductionError(Exception):
    pass


def complete(member):
    """The member has read the agreement and the rules and agrees to them."""
    with transaction.atomic():
        member = Member.objects.select_for_update().get(pk=member.pk)
        before = changes.snapshot(member)
        member.induction_completed = True
        member.rules_agreed_at = member.rules_agreed_at or timezone.now()
        member.save(update_fields=["induction_completed", "rules_agreed_at", "updated_at"])
        changes.record(member, before, changed_by=member)
    return member


def waiting():
    """Members who've done the induction online and are waiting for it to be confirmed, longest-standing first.

    As legacy did, members who have left aren't listed.
    """
    return (
        Member.objects.filter(induction_completed=True, inducted_by__isnull=True, inducted_at__isnull=True)
        .exclude(status=Member.Status.LEFT)
        .order_by("date_joined", "pk")
    )


def confirm(member, confirmed_by):
    """Confirm a member's general induction. InductionError if they haven't done it or it's already confirmed."""
    with transaction.atomic():
        member = Member.objects.select_for_update().get(pk=member.pk)
        if not member.induction_completed:
            raise InductionError(f"{member.full_name} hasn't done the general induction yet.")
        if member.inducted_by_id or member.inducted_at:
            raise InductionError(f"{member.full_name}'s induction has already been confirmed.")
        before = changes.snapshot(member)
        member.inducted_by = confirmed_by
        member.inducted_at = timezone.now()
        member.save(update_fields=["inducted_by", "inducted_at", "updated_at"])
        changes.record(member, before, changed_by=confirmed_by)
    return member
