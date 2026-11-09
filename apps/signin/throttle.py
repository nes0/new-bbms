"""
Rate limits for logging in, password resets and two-factor codes (spec 06 §6.11).

Each limit is "at most N in the last M minutes" for one key: an email address, an IP address or a member. Keys are
hashed before they're stored. A limit by email applies whether or not there's an account with that address, so the
messages never reveal who has an account.

Limits by IP address only apply when `CLIENT_IP_HEADER` names the header the hosting's proxy puts the visitor's
address in. Without it every visitor seems to come from the proxy, and an IP limit would lock everyone out at once.
"""

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import Attempt

KEEP = timedelta(days=1)  # older attempts are deleted


@dataclass(frozen=True)
class Limit:
    most: int
    minutes: int


LIMITS = {
    "login-email": Limit(5, 15),  # failed logins for one email address
    "login-ip": Limit(30, 15),  # failed logins from one address
    "reset-email": Limit(3, 60),  # password reset emails asked for one address
    "reset-ip": Limit(10, 60),
    "two-factor": Limit(5, 15),  # wrong codes for one member
}


def _key(kind: str, value) -> str:
    return hashlib.sha256(f"{settings.SECRET_KEY}:{kind}:{str(value).strip().lower()}".encode()).hexdigest()


def client_ip(request) -> str:
    """The visitor's address, or "" if we can't trust one (see the module's notes)."""
    header = settings.CLIENT_IP_HEADER
    if not header:
        return ""
    return request.META.get(header, "").split(",")[0].strip()


def blocked(kind: str, value) -> bool:
    if not value:
        return False
    limit = LIMITS[kind]
    since = timezone.now() - timedelta(minutes=limit.minutes)
    return Attempt.objects.filter(kind=kind, key=_key(kind, value), at__gte=since).count() >= limit.most


def record(kind: str, value):
    if not value:
        return
    now = timezone.now()
    Attempt.objects.create(kind=kind, key=_key(kind, value), at=now)
    Attempt.objects.filter(at__lt=now - KEEP).delete()


def clear(kind: str, value):
    if value:
        Attempt.objects.filter(kind=kind, key=_key(kind, value)).delete()


def minutes(kind: str) -> int:
    return LIMITS[kind].minutes
