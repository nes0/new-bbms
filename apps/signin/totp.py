"""Time-based one-time codes (TOTP, RFC 6238), as made by authenticator apps: 6 digits, 30 seconds, SHA-1."""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

STEP = 30
DIGITS = 6
WINDOW = 1  # also accept the code just before and just after, for clocks a little out


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode()


def code_at(secret: str, step: int) -> str:
    key = base64.b32decode(secret)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**DIGITS).zfill(DIGITS)


def current_step(now=None) -> int:
    return int((now if now is not None else time.time()) // STEP)


def check(secret: str, code: str, last_step: int = 0, now=None) -> int | None:
    """The step `code` is valid for (to remember, so it can't be used again), or None."""
    code = "".join(code.split())
    if not (code.isdigit() and len(code) == DIGITS):
        return None
    step = current_step(now)
    for candidate in range(step - WINDOW, step + WINDOW + 1):
        if candidate > last_step and hmac.compare_digest(code_at(secret, candidate), code):
            return candidate
    return None


def setup_uri(secret: str, account: str, issuer: str) -> str:
    """What the QR code holds: authenticator apps read it to add the account."""
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?{urlencode({'secret': secret, 'issuer': issuer})}"
