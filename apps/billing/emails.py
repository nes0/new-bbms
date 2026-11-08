"""
Emails about billing and membership (spec 03 §3.2, §3.6; 06 §6.5).

Each email is a plain-text template in templates/billing/emails/ (or, for another app's emails, the path given, e.g.
"members/emails/trusted"), whose first line is the subject. Nothing is sent
during a dry run (`suppressed()`), so the shadow site and `--dry-run` jobs can't email anyone (08).
"""

import contextvars
import logging

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.mail import send_mail
from django.template.loader import render_to_string

from apps.site_settings.models import SiteSettings

logger = logging.getLogger(__name__)

_suppressed = contextvars.ContextVar("billing_emails_suppressed", default=False)
# The role whose members look after lockers and boxes (06 §6.2).
STORAGE_TEAM = "storage"


class suppressed:
    def __enter__(self):
        self._token = _suppressed.set(True)

    def __exit__(self, *exc):
        _suppressed.reset(self._token)


def is_suppressed() -> bool:
    """Whether this is a dry run (nothing may be sent anywhere)."""
    return _suppressed.get()


def _render(template, context):
    context = {"site_name": SiteSettings.load().space_name, "site_url": settings.SITE_URL, **context}
    path = template if "/" in template else f"billing/emails/{template}"
    text = render_to_string(f"{path}.txt", context).strip()
    subject, _, body = text.partition("\n")
    return subject.strip(), body.strip() + "\n"


def _deliver(subject, body, recipients, what) -> bool:
    """Hand an email to the mail server. A failure is logged and does not stop the caller: an email that couldn't be
    sent must never undo or halt a payment, a status change or a nightly job (a Direct Debit may already have been
    asked for). Only the kind of failure is logged, never the address or the message."""
    try:
        send_mail(subject, body, None, recipients)
    except Exception as error:  # noqa: BLE001 - any failure to send is survivable
        logger.error("Couldn't send an email (%s): %s", what, type(error).__name__)
        return False
    return True


def send(member, template, **context):
    """Email one member. Logs the member's id and the template, never the content."""
    if _suppressed.get():
        logger.info("Dry run: would email member %s (%s)", member.pk, template)
        return
    subject, body = _render(template, {"member": member, **context})
    if _deliver(subject, body, [member.email], f"member {member.pk}, {template}"):
        logger.info("Emailed member %s (%s)", member.pk, template)


def send_to_address(address, template, **context):
    """Email someone who isn't a member (a PayPal payer BBMS doesn't know, say). Logs the template only."""
    if _suppressed.get():
        logger.info("Dry run: would email a non-member (%s)", template)
        return
    subject, body = _render(template, context)
    if _deliver(subject, body, [address], f"a non-member, {template}"):
        logger.info("Emailed a non-member (%s)", template)


def tell_role(role, template, **context):
    """Email everyone in a role (e.g. "finance"), if it exists."""
    team = Group.objects.filter(name=role).first()
    emails = list(team.user_set.filter(is_active=True).values_list("email", flat=True)) if team else []
    if not emails:
        logger.warning("No %s role holders to tell (%s)", role, template)
        return
    if _suppressed.get():
        logger.info("Dry run: would email the %s role (%s)", role, template)
        return
    subject, body = _render(template, context)
    _deliver(subject, body, emails, f"the {role} role, {template}")


def tell_storage_team(template, **context):
    """Email everyone in the storage team's role, if it exists."""
    tell_role(STORAGE_TEAM, template, **context)
