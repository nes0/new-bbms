"""
The members' audit log and what follows a change to their flags (spec 02 §2.3, 03 §3.9).

Changes to the flags below are logged with who made them, wherever they're made: the admin, the general induction
pages, or the member's own induction. Take a `snapshot()` before changing a member and pass it to `record()` after
saving.
"""

import logging

from django.utils import timezone

from apps.billing import emails

from .models import Member, MemberChange

logger = logging.getLogger(__name__)

# The spec's audited fields (trusted, key holder, induction completed), plus the photo flags (as legacy did) and
# banned. Who confirmed a general induction is logged as "induction confirmed by".
AUDITED_FIELDS = (
    "trusted",
    "key_holder",
    "induction_completed",
    "inducted_by",
    "photo_approved",
    "photo_on_wall",
    "banned",
)


def snapshot(member):
    return {name: getattr(member, name) for name in AUDITED_FIELDS}


def _shown(value):
    if isinstance(value, bool):
        return "yes" if value else "no"
    return "" if value is None else str(value)


def record(member, before, changed_by):
    """Log each audited field that changed since `before`, and tell a member who has just become trusted."""
    now = timezone.now()
    after = snapshot(member)
    changes = [
        MemberChange(
            member=member,
            field=Member._meta.get_field(name).verbose_name,
            old_value=_shown(before[name])[:255],
            new_value=_shown(after[name])[:255],
            changed_by=changed_by,
            changed_at=now,
        )
        for name in AUDITED_FIELDS
        if before[name] != after[name]
    ]
    MemberChange.objects.bulk_create(changes)
    if changes:
        logger.info(
            "Member %s changed by %s: %s",
            member.pk,
            changed_by.pk if changed_by else None,
            ", ".join(c.field for c in changes),
        )
    if after["trusted"] and not before["trusted"]:
        # TODO(notifications app, 06 §6.5): also an in-app notification, deduplicated as "trusted_status".
        emails.send(member, "members/emails/trusted")
    return changes
