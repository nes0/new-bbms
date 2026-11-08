"""
Claiming, approving and declining expenses (spec 06 §6.4).

- **Claim**: members (anyone who sees the member directory) claim for themselves only, with a receipt (JPEG, PNG or
  PDF). Photos of receipts are re-saved, which drops the camera's metadata (e.g. where it was taken). Everyone who
  can approve expenses is told (in BBMS and by email).
- **Approve** (the "approve expenses" permission; admins have it): pays the amount into the member's credit, once.
  Nobody approves their own claim.
- **Decline**: with a reason, which the member is sent.
- A member can **withdraw** a claim that's still waiting; it's deleted with its receipt.
- **Who sees what**: members see their own claims; those who can approve, and those with "see all expenses" (the
  finance role), see everyone's.
"""

import io
import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from PIL import Image, ImageOps, UnidentifiedImageError

from apps.billing import emails
from apps.notifications.notify import notify
from apps.payments import services
from apps.payments.models import Payment

from .models import Expense

logger = logging.getLogger(__name__)

MAX_RECEIPT_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 50_000_000
PDF_SIGNATURE = b"%PDF-"


class BadReceipt(Exception):
    """The upload isn't a receipt BBMS can keep. The message is shown to the member."""


class CantDecide(Exception):
    """Why an expense couldn't be approved or declined. The message is shown."""


# -- Who can do what ----------------------------------------------------------------------------------------------


def can_approve(user) -> bool:
    return user.is_authenticated and user.has_perm("expenses.approve_expense")


def can_see_all(user) -> bool:
    return user.is_authenticated and (user.has_perm("expenses.see_all_expenses") or can_approve(user))


def can_see(user, expense) -> bool:
    return user.is_authenticated and (expense.member_id == user.pk or can_see_all(user))


def approvers():
    """Everyone who can approve expenses: superusers, and members given the permission directly or by a role."""
    permission = Permission.objects.get(content_type__app_label="expenses", codename="approve_expense")
    condition = Q(is_superuser=True) | Q(user_permissions=permission) | Q(groups__permissions=permission)
    return get_user_model().objects.filter(condition, is_active=True).distinct()


# -- Receipts -----------------------------------------------------------------------------------------------------


def receipt_file(upload) -> ContentFile:
    """The receipt to keep, from an uploaded file: a PDF as it is, a photo re-saved. Raises BadReceipt."""
    if upload.size > MAX_RECEIPT_BYTES:
        raise BadReceipt(f"That file is too big: please send one under {MAX_RECEIPT_BYTES // 1024 // 1024} MB.")
    head = upload.read(len(PDF_SIGNATURE))
    upload.seek(0)
    if head == PDF_SIGNATURE:
        return ContentFile(upload.read(), name="receipt.pdf")
    try:
        image = Image.open(upload)
        kind = image.format
        if kind not in ("JPEG", "PNG"):
            raise BadReceipt("Please send the receipt as a JPEG, PNG or PDF.")
        if image.width * image.height > MAX_PIXELS:
            raise BadReceipt("That photo is too big. Please send a smaller one.")
        image = ImageOps.exif_transpose(image)
        out = io.BytesIO()
        if kind == "PNG" or image.mode in ("RGBA", "LA", "P"):
            image.save(out, "PNG", optimize=True)
            return ContentFile(out.getvalue(), name="receipt.png")
        image.convert("RGB").save(out, "JPEG", quality=85)
        return ContentFile(out.getvalue(), name="receipt.jpg")
    except BadReceipt:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as error:
        logger.info("An expense receipt couldn't be read: %s", error)
        raise BadReceipt("That doesn't look like a receipt we can read. Please send a JPEG, PNG or PDF.") from error


# -- Claiming -----------------------------------------------------------------------------------------------------


def _link(expense) -> str:
    return reverse("expenses:detail", args=[expense.pk])


def submit(member, *, category, description, amount_pence, spent_on, receipt) -> Expense:
    """Record a claim. `receipt` is what `receipt_file` returned."""
    expense = Expense(
        member=member, category=category, description=description, amount_pence=amount_pence, spent_on=spent_on
    )
    expense.receipt.save(receipt.name, receipt, save=False)
    expense.save()
    logger.info("Member %s claimed expense %s", member.pk, expense.pk)

    def tell_approvers():
        for approver in approvers().exclude(pk=member.pk):
            notify(
                approver,
                f"{member.full_name} claimed £{amount_pence / 100:.2f} for {expense.get_category_display().lower()}.",
                kind="expense_submitted",
                key=f"{expense.pk}-expense_submitted",
                url=_link(expense),
            )
            emails.send(approver, "expenses/emails/submitted", expense=expense, claimant=member, link=_link(expense))

    transaction.on_commit(tell_approvers)
    return expense


def withdraw(expense, member):
    """The member takes back a claim that's still waiting: it's deleted, with its receipt."""
    with transaction.atomic():
        expense = Expense.objects.select_for_update().get(pk=expense.pk)
        if expense.member_id != member.pk or expense.status != Expense.Status.WAITING:
            raise CantDecide("Only a claim still waiting for approval can be withdrawn.")
        receipt = expense.receipt
        expense.delete()
    if receipt:
        receipt.storage.delete(receipt.name)


# -- Deciding -----------------------------------------------------------------------------------------------------


def _decide(expense_id, by, status, *, reason="") -> Expense:
    if not can_approve(by):
        raise CantDecide("You can't approve or decline expenses.")
    with transaction.atomic():
        expense = Expense.objects.select_for_update().select_related("member").get(pk=expense_id)
        if expense.status != Expense.Status.WAITING:
            raise CantDecide(f"This expense has already been {expense.get_status_display().lower()}.")
        if expense.member_id == by.pk:
            raise CantDecide("Someone else must decide on your own expense.")
        if status == Expense.Status.APPROVED:
            expense.payment = services.record_payment(
                expense.member,
                Payment.Reason.BALANCE,
                Payment.Source.REIMBURSEMENT,
                expense.amount_pence,
                reference=str(expense.pk),
                created_by=by,
            )
        expense.status, expense.decided_by, expense.decided_at = status, by, timezone.now()
        expense.decline_reason = reason
        expense.save()
    logger.info("Expense %s %s by member %s", expense.pk, status, by.pk)
    return expense


def approve(expense_id, by) -> Expense:
    """Pay the expense into the member's credit, and tell them. Raises CantDecide."""
    expense = _decide(expense_id, by, Expense.Status.APPROVED)
    amount = f"£{expense.amount_pence / 100:.2f}"
    notify(
        expense.member,
        f"Your expense of {amount} was approved and added to your credit.",
        kind="expense_approved",
        key=f"{expense.pk}-expense_approved",  # legacy's key, so an imported notification isn't repeated
        url=_link(expense),
    )
    emails.send(expense.member, "expenses/emails/approved", expense=expense, link=_link(expense))
    return expense


def decline(expense_id, by, reason) -> Expense:
    """Decline the expense with a reason, and tell the member. Raises CantDecide."""
    reason = (reason or "").strip()
    if not reason:
        raise CantDecide("Please say why, so the member knows.")
    expense = _decide(expense_id, by, Expense.Status.DECLINED, reason=reason)
    notify(
        expense.member,
        f"Your expense of £{expense.amount_pence / 100:.2f} was declined.",
        kind="expense_declined",
        key=f"{expense.pk}-expense_declined",
        url=_link(expense),
    )
    emails.send(expense.member, "expenses/emails/declined", expense=expense, link=_link(expense))
    return expense
