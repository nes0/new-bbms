"""
Sending a group email (spec 06 §6.6).

One plain-text email, Bcc'd to the group in batches (so nobody sees anyone else's address, and a batch stays under
mail providers' recipient limits), with replies going to the sender. The footer says who sent it and why the reader
got it. Nothing is sent during a dry run (the shadow site, 08).
"""

import logging

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.template.loader import render_to_string

from apps.billing import emails
from apps.site_settings.models import SiteSettings

from . import audiences
from .models import GroupEmail

logger = logging.getLogger(__name__)

BATCH = 50


def _message(sender, subject, body, label, *, test=False):
    context = {
        "sender": sender,
        "body": body.strip(),
        "label": label,
        "test": test,
        "site_name": SiteSettings.load().space_name,
        "site_url": settings.SITE_URL,
    }
    text = render_to_string("group_email/emails/group_email.txt", context).strip() + "\n"
    return EmailMessage(
        subject=f"[Test] {subject}" if test else subject, body=text, reply_to=[sender.email], to=[], bcc=[]
    )


def send_test(sender, key: str, subject: str, body: str) -> bool:
    """Send the email to the sender only, as the group would get it. Returns whether the mail server took it."""
    message = _message(sender, subject, body, audiences.label(key), test=True)
    message.to = [sender.email]
    if emails.is_suppressed():
        return True
    try:
        message.send()
    except Exception as error:  # noqa: BLE001 - shown to the sender, who can try again
        logger.error("Couldn't send a test group email: %s", type(error).__name__)
        return False
    return True


def send(sender, key: str, subject: str, body: str) -> GroupEmail:
    """Email everyone in the group and keep a record. The caller has checked the sender may email this group."""
    label = audiences.label(key)
    addresses = sorted(set(audiences.members_of(key).values_list("email", flat=True)))
    record = GroupEmail.objects.create(
        sent_by=sender, audience=key, audience_label=label, subject=subject, body=body, recipients=len(addresses)
    )
    if emails.is_suppressed():
        logger.info("Dry run: would send group email %s to %s members", record.pk, len(addresses))
        return record
    connection = get_connection()
    failed = 0
    for start in range(0, len(addresses), BATCH):
        batch = addresses[start : start + BATCH]
        message = _message(sender, subject, body, label)
        message.bcc, message.connection = batch, connection
        try:
            message.send()
        except Exception as error:  # noqa: BLE001 - the other batches still go; the count says what didn't
            logger.error("Group email %s: a batch of %s wasn't sent: %s", record.pk, len(batch), type(error).__name__)
            failed += len(batch)
    connection.close()
    if failed:
        GroupEmail.objects.filter(pk=record.pk).update(failed=failed)
        record.failed = failed
    logger.info("Group email %s sent to %s members by member %s", record.pk, len(addresses) - failed, sender.pk)
    return record
