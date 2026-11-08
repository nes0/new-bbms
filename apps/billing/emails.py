"""
Emails about billing and membership (spec 03 §3.2, §3.6; 06 §6.5).

Each email is a plain-text template in templates/billing/emails/, whose first line is the subject. Nothing is sent
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


def _render(template, context):
    context = {"site_name": SiteSettings.load().space_name, "site_url": settings.SITE_URL, **context}
    text = render_to_string(f"billing/emails/{template}.txt", context).strip()
    subject, _, body = text.partition("\n")
    return subject.strip(), body.strip() + "\n"


def send(member, template, **context):
    """Email one member. Logs the member's id and the template, never the content."""
    if _suppressed.get():
        logger.info("Dry run: would email member %s (%s)", member.pk, template)
        return
    subject, body = _render(template, {"member": member, **context})
    send_mail(subject, body, None, [member.email])
    logger.info("Emailed member %s (%s)", member.pk, template)


def tell_storage_team(template, **context):
    """Email everyone in the storage team's role, if it exists."""
    team = Group.objects.filter(name=STORAGE_TEAM).first()
    emails = list(team.user_set.filter(is_active=True).values_list("email", flat=True)) if team else []
    if not emails:
        logger.warning("No %s team to tell (%s)", STORAGE_TEAM, template)
        return
    if _suppressed.get():
        logger.info("Dry run: would email the %s team (%s)", STORAGE_TEAM, template)
        return
    subject, body = _render(template, context)
    send_mail(subject, body, None, emails)
