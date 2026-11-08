"""
Telling members things inside BBMS (spec 06 §6.5).

Everything that wants to tell a member something calls `notify`. A `key` stops the same thing being said twice
(legacy's `hash`); leave it out for something that can happen again. Notifications are plain text: the message is
escaped when shown, and `url` can only be a page on this site.
"""

import logging

from django.contrib.auth import get_user_model
from django.db.models import Q

from .models import Notification

logger = logging.getLogger(__name__)

ADMIN_ROLE = "admin"


def _site_path(url: str) -> str:
    """A link must be a page on this site (starting with one `/`), so a notification can't send anyone elsewhere."""
    if url and not (url.startswith("/") and not url.startswith("//")):
        raise ValueError("A notification's link must be a path on this site, like /pay/.")
    return url


def notify(member, message: str, *, kind: str = "", key: str = "", url: str = ""):
    """Add a notification to the member's list. Returns it, or None if one with this key was already sent."""
    url = _site_path(url)
    fields = {"kind": kind[:100], "message": message, "url": url}
    if key:
        notification, created = Notification.objects.get_or_create(member=member, key=key[:150], defaults=fields)
        return notification if created else None
    return Notification.objects.create(member=member, **fields)


def notify_many(members, message: str, **options) -> int:
    """`notify` for each member. Returns how many were told (not counting repeats)."""
    return sum(1 for member in members if notify(member, message, **options) is not None)


def role_members(*roles: str, admins: bool = False):
    """Active members holding any of these roles, and (with `admins`) the admins: superusers and the `admin` role."""
    condition = Q(groups__name__in=roles)
    if admins:
        condition |= Q(is_superuser=True) | Q(groups__name=ADMIN_ROLE)
    return get_user_model().objects.filter(condition, is_active=True).distinct()


def notify_role(*roles: str, message: str, admins: bool = False, **options) -> int:
    """Tell everyone in these roles (and the admins, if asked). Returns how many were told."""
    members = list(role_members(*roles, admins=admins))
    if not members:
        logger.warning("No one holds %s to tell: %s", "/".join(roles) or "the admin role", options.get("kind", ""))
    return notify_many(members, message, **options)


def unread_count(member) -> int:
    return Notification.objects.filter(member=member, unread=True).count()
