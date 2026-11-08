"""
Confirming that a member can read mail sent to their address (spec 03 §3.1, "Confirming the email address").

The address is the login and where every email goes, and "Log in with BBMS" tells Moodle who someone is by it, so
BBMS must know it's really theirs. The member is emailed a link; following it proves they can read that mailbox.

The link carries a **signed token** (nothing is stored): who it's for, the address, what it's for, and when it was
made. It stops working after a week, and a link for an address the member has since changed does nothing.
- "verify": confirms the address the member has now.
- "change": the member asked to log in with a new address. The link goes to the NEW address, and following it makes
  that the member's address (so a typo, or someone else's address, can never take over an account).
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.billing import emails

from .models import Member

logger = logging.getLogger(__name__)

SALT = "bbms.members.email-confirmation"
MAX_AGE = timedelta(days=7)

# What following a link did.
CONFIRMED = "confirmed"
CHANGED = "changed"  # the member's address is now the new one (and confirmed)
ALREADY = "already"
INVALID = "invalid"  # not a real link, too old, or for an address the member no longer has
TAKEN = "taken"  # the new address now belongs to another account


def _link(kind, member, email) -> str:
    token = signing.dumps({"m": member.pk, "e": email, "k": kind}, salt=SALT)
    return settings.SITE_URL + reverse("members:confirm-email", args=[token])


def send_confirmation(member) -> None:
    """Email the member a link to confirm the address they have. An email problem is logged, not raised."""
    emails.send(member, "members/emails/confirm_email", link=_link("verify", member, member.email))


def send_change_request(member, new_email: str) -> None:
    """The member wants to log in with `new_email`. The link goes to that address, and the current one is warned."""
    emails.send_to_address(
        new_email,
        "members/emails/confirm_new_email",
        member=member,
        link=_link("change", member, new_email),
        days=MAX_AGE.days,
    )
    emails.send(member, "members/emails/email_change_asked")


def confirm(token: str, now=None):
    """Follow a link. Returns (outcome, member or None)."""
    try:
        data = signing.loads(token, salt=SALT, max_age=MAX_AGE.total_seconds())
        member = Member.objects.get(pk=data["m"])
        email, kind = data["e"], data["k"]
    except (signing.BadSignature, Member.DoesNotExist, KeyError, TypeError):
        return INVALID, None
    now = now or timezone.now()
    if kind == "verify":
        if member.email != email:
            return INVALID, None
        if member.email_verified_at:
            return ALREADY, member
        member.email_verified_at = now
        member.save(update_fields=["email_verified_at"])
        return CONFIRMED, member
    if kind == "change":
        if member.email == email:
            return ALREADY, member
        old = member.email
        member.email, member.email_verified_at = email, now
        try:
            with transaction.atomic():  # so a clash can be handled without breaking the caller's transaction
                member.save(update_fields=["email", "email_verified_at"])
        except IntegrityError:  # someone else took it since the link was sent
            return TAKEN, None
        # Warn the address that used to be theirs, in case it wasn't them.
        emails.send_to_address(old, "members/emails/email_changed", member=member, new_email=email)
        logger.info("Member %s changed their login email address", member.pk)
        return CHANGED, member
    return INVALID, None


def address_in_use(email: str, except_member=None) -> bool:
    found = Member.objects.filter(email__iexact=email)
    if except_member is not None:
        found = found.exclude(pk=except_member.pk)
    return found.exists()
