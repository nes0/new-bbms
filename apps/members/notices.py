"""
What members, and the trustees, are told about address changes and proof-of-identity checks (spec 03 §3.1, §3.9;
06 §6.5). The wording lives here so the rules code just says what happened.
"""

from django.urls import reverse

from apps.billing import emails
from apps.notifications.notify import notify, notify_role

TRUSTEES = "trustees"


def address_waiting(address):
    """A member has asked to change their address: the trustees (and admins) have one to approve."""
    notify_role(
        TRUSTEES,
        admins=True,
        message=f"{address.member.get_short_name()} has changed their address. It needs approving.",
        kind="address_waiting",
        key=f"address-waiting-{address.pk}",
        url=reverse("admin:members_address_changelist") + "?approved__exact=0",
    )


def address_approved(member):
    notify(member, "Your new address has been approved.", kind="address_approved", url="/account/")


def address_declined(member):
    notify(
        member,
        "Your change of address wasn't accepted, so we still have your old one. "
        "Please check it and send it again, or ask a trustee.",
        kind="address_declined",
        url="/account/address/",
    )


def id_checked(check):
    """Tell the member how their proof of identity went: approved, or please send it again (with the trustee's note)."""
    member = check.member
    if check.result == check.Result.APPROVED:
        emails.send(member, "members/emails/id_approved")
        notify(member, "Thanks: a trustee has checked your proof of identity.", kind="id_approved", url="/account/")
    else:
        emails.send(member, "members/emails/id_rejected", note=check.note)
        message = "A trustee couldn't accept your proof of identity, so please send it again."
        notify(
            member,
            f"{message} Their note: {check.note}" if check.note else message,
            kind="id_rejected",
            url=reverse("members:id-documents"),
        )
