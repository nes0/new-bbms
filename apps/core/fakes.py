"""
The pretend GoCardless, Stripe, PayPal, Discord and Moodle, and the fake device pages, are used when developing
(DEBUG) and on a demo site for trying BBMS out (DEMO_SITE). Never on the live site.
"""

from pathlib import Path

from django.conf import settings


def allowed() -> bool:
    """Whether the fakes may be used (checked at call time, so tests can switch DEBUG)."""
    return bool(settings.DEBUG or settings.DEMO_SITE)


def state_file(name: str) -> str:
    """Where a fake keeps its state between requests (FAKES_DIR: the project folder unless set)."""
    return str(Path(settings.FAKES_DIR) / name)
