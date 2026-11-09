"""
Two-factor login (spec 06 §6.11, "optional 2FA for admins"): a code from an authenticator app as well as the password.

Any member can turn it on; a setting can require it for the admin site. Each member also gets ten one-off recovery
codes, for when their phone is lost. An admin can switch a member's two-factor off in the admin (Logging in →
Two-factor logins → delete) once they've checked who's asking.
"""

import hashlib
import hmac
import secrets

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import totp
from .models import RecoveryCode, TwoFactor

RECOVERY_CODES = 10


def is_on(member) -> bool:
    return TwoFactor.objects.filter(member=member, confirmed_at__isnull=False).exists()


def start(member) -> TwoFactor:
    """A new secret to show as a QR code. Replaces any setup that wasn't finished; refuses if it's already on."""
    if is_on(member):
        raise ValueError("Two-factor login is already on.")
    device, _ = TwoFactor.objects.update_or_create(
        member=member, defaults={"secret": totp.new_secret(), "created_at": timezone.now(), "last_step": 0}
    )
    return device


def _hash(code: str) -> str:
    normalised = "".join(code.split()).replace("-", "").upper()
    return hmac.new(settings.SECRET_KEY.encode(), normalised.encode(), hashlib.sha256).hexdigest()


@transaction.atomic
def new_recovery_codes(member) -> list[str]:
    """Ten new codes (any old ones stop working). Shown once; only hashes are kept."""
    RecoveryCode.objects.filter(member=member).delete()
    codes = []
    for _ in range(RECOVERY_CODES):
        raw = secrets.token_hex(5).upper()  # 10 characters, 40 bits each
        codes.append(f"{raw[:5]}-{raw[5:]}")
    RecoveryCode.objects.bulk_create([RecoveryCode(member=member, code_hash=_hash(code)) for code in codes])
    return codes


@transaction.atomic
def confirm(member, code: str) -> list[str] | None:
    """Turn it on with a first code from the app. Returns the recovery codes, or None if the code was wrong."""
    device = TwoFactor.objects.select_for_update().filter(member=member, confirmed_at__isnull=True).first()
    if device is None:
        return None
    step = totp.check(device.secret, code, device.last_step)
    if step is None:
        return None
    device.confirmed_at, device.last_step = timezone.now(), step
    device.save(update_fields=["confirmed_at", "last_step"])
    return new_recovery_codes(member)


@transaction.atomic
def verify(member, code: str) -> str:
    """Check a code at login: "app", "recovery" (a recovery code, now used up) or "" if it's wrong."""
    device = TwoFactor.objects.select_for_update().filter(member=member, confirmed_at__isnull=False).first()
    if device is None:
        return ""
    step = totp.check(device.secret, code, device.last_step)
    if step is not None:
        device.last_step = step
        device.save(update_fields=["last_step"])
        return "app"
    recovery = RecoveryCode.objects.filter(member=member, code_hash=_hash(code), used_at__isnull=True).first()
    if recovery is not None:
        recovery.used_at = timezone.now()
        recovery.save(update_fields=["used_at"])
        return "recovery"
    return ""


def recovery_codes_left(member) -> int:
    return RecoveryCode.objects.filter(member=member, used_at__isnull=True).count()


@transaction.atomic
def turn_off(member):
    TwoFactor.objects.filter(member=member).delete()
    RecoveryCode.objects.filter(member=member).delete()
