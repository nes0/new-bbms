"""
The members' audit log and what follows a change to their flags (spec 02 §2.3, 03 §3.9).

Changes to the flags below are logged with who made them, wherever they're made: the admin, the general induction
pages, or the member's own induction. Take a `snapshot()` before changing a member and pass it to `record()` after
saving.
"""

import logging

from django.utils import timezone

from apps.billing import emails
from apps.notifications.notify import notify

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
    "email_verified",  # a property: whether their email address is confirmed (03 §3.1)
)
LABELS = {"email_verified": "email address confirmed"}  # for audited names that aren't model fields


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
            field=LABELS.get(name) or Member._meta.get_field(name).verbose_name,
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
    if before["email_verified"] and not after["email_verified"]:
        # Withdrawn by an admin, or their address was changed for them: until it's confirmed again they can't pay, be
        # signed off or use the door, so say so (03 §3.1).
        notify(
            member,
            "Your email address needs confirming again, until then you can't pay, be signed off or use the door "
            "key. Use the link on your account page.",
            kind="email_unconfirmed",
            url="/account/",
        )
    if after["trusted"] and not before["trusted"]:
        emails.send(member, "members/emails/trusted")
        # Once only, even if they're made trusted again later (06 §6.5).
        notify(
            member,
            "You're now a trusted member. Pay the key deposit to get a key.",
            kind="trusted_status",
            key="trusted_status",
            url="/pay/",
        )
    return changes
