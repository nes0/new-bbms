"""
PayPal: subscriptions and credit top-ups (spec 04 §4.4).

BBMS uses PayPal's payment buttons (Payments Standard) and its notifications (IPN), so it needs no API credentials:
- a member is sent to PayPal with a link BBMS builds (`subscribe_url`, `top_up_url`), which pays the space's account
  (`PAYPAL_ACCOUNT_EMAIL`) and tells PayPal to notify `/paypal-ipn`;
- every notification is sent back to PayPal to confirm it's genuine (`verify`) before anything is recorded.
  The rules for what a notification does are in `paypal_ipn`.

Without `PAYPAL_ACCOUNT_EMAIL`, development and tests use a fake: the "PayPal page" is a local page (`/dev/paypal/`,
DEBUG only) that sends BBMS made-up notifications, and nothing is checked with PayPal. A live site without it
shows no PayPal buttons and refuses notifications.
"""

import codecs
import logging
import urllib.request
from urllib.parse import parse_qsl, urlencode

from django.conf import settings
from django.core import signing
from django.urls import reverse

from apps.core import fakes
from apps.site_settings.models import SiteSettings

logger = logging.getLogger(__name__)

FAKE_ACCOUNT = "paypal-fake@example.com"
CHECKOUT = {"live": "https://www.paypal.com/cgi-bin/webscr", "sandbox": "https://www.sandbox.paypal.com/cgi-bin/webscr"}
VERIFY = {
    "live": "https://ipnpb.paypal.com/cgi-bin/webscr",
    "sandbox": "https://ipnpb.sandbox.paypal.com/cgi-bin/webscr",
}
_SALT = "bbms.paypal.member"


class Unreachable(Exception):
    """PayPal couldn't be asked whether a notification is genuine. PayPal sends it again later."""


def using_fake() -> bool:
    return not settings.PAYPAL_ACCOUNT_EMAIL and (fakes.allowed() or getattr(settings, "TESTING", False))


def account_email() -> str:
    """The space's PayPal account: what every button pays. Empty if PayPal isn't set up."""
    if settings.PAYPAL_ACCOUNT_EMAIL:
        return settings.PAYPAL_ACCOUNT_EMAIL.strip().lower()
    return FAKE_ACCOUNT if using_fake() else ""


def available() -> bool:
    return bool(account_email())


def accepted_accounts() -> set[str]:
    """Accounts whose payments are recorded: the current one and, during a switch, the old ones (04 §4.4)."""
    old = {email.strip().lower() for email in settings.PAYPAL_OLD_ACCOUNT_EMAILS if email.strip()}
    return {account_email(), *old} - {""}


def _mode():
    return "sandbox" if settings.PAYPAL_SANDBOX else "live"


# --- who a payment is from ---


def member_reference(member) -> str:
    """Put on every button (PayPal's `custom` field), so a payment can be matched even from another email address."""
    return signing.Signer(salt=_SALT).sign(str(member.pk))


def member_id_from(reference: str) -> int | None:
    try:
        return int(signing.Signer(salt=_SALT).unsign(reference))
    except (signing.BadSignature, ValueError):
        return None


# --- sending a member to PayPal ---


def _url(params) -> str:
    base = reverse("payments:fake-paypal") if using_fake() else CHECKOUT[_mode()]
    return f"{base}?{urlencode(params)}"


def _common(member, notify_url, return_url, cancel_url):
    return {
        "business": account_email(),
        "currency_code": "GBP",
        "lc": "GB",
        "charset": "utf-8",
        "no_note": "1",
        "no_shipping": "1",
        "custom": member_reference(member),
        "notify_url": notify_url,
        "return": return_url,
        "cancel_return": cancel_url,
    }


def subscribe_url(member, amount_pence, *, notify_url, return_url, cancel_url) -> str:
    """PayPal's page for a monthly subscription of this amount."""
    return _url(
        {
            "cmd": "_xclick-subscriptions",
            "item_name": f"{SiteSettings.load().space_name} membership",
            "a3": f"{amount_pence / 100:.2f}",
            "p3": "1",
            "t3": "M",
            "src": "1",  # keeps going every month until cancelled
            **_common(member, notify_url, return_url, cancel_url),
        }
    )


def top_up_url(member, amount_pence, *, notify_url, return_url, cancel_url) -> str:
    """PayPal's page for a one-off payment into the member's credit."""
    return _url(
        {
            "cmd": "_xclick",
            "item_name": f"{SiteSettings.load().space_name} credit top-up",
            "amount": f"{amount_pence / 100:.2f}",
            **_common(member, notify_url, return_url, cancel_url),
        }
    )


# --- notifications ---


def parse(raw: bytes) -> dict:
    """A notification's fields. PayPal says which character set it used in the `charset` field."""
    charset = "windows-1252"  # PayPal's default
    for name, value in parse_qsl(raw.decode("latin-1"), keep_blank_values=True):
        if name == "charset":
            charset = value
    try:
        codecs.lookup(charset)
    except LookupError:
        charset = "windows-1252"
    # The body is percent-encoded; the character set applies to the bytes the escapes stand for.
    return dict(parse_qsl(raw.decode("latin-1"), keep_blank_values=True, encoding=charset, errors="replace"))


def verify(raw: bytes) -> bool:
    """Ask PayPal whether it really sent this notification, sending it back exactly as received."""
    if using_fake():
        return True
    request = urllib.request.Request(
        VERIFY[_mode()],
        data=b"cmd=_notify-validate&" + raw,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "BBMS-IPN-check"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            answer = response.read().strip()
    except OSError as error:
        logger.warning("Couldn't check a PayPal notification with PayPal: %s", error)
        raise Unreachable from error
    return answer == b"VERIFIED"
