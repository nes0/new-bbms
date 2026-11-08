"""
Member photos (spec 06 §6.1): uploading, approving, and who may see them.

- A member uploads a photo; it's cropped to a square from the middle, made 500 px and 200 px (PNGs, as legacy), and
  waits for approval (one at a time: a new upload replaces a waiting one).
- An admin approves it (it replaces the current photo, and the member has an approved photo for the door, 05 §5.2)
  or declines it (the member is emailed).
- Photos are in private storage and only served through BBMS, which checks who's asking (`can_see`).
"""

import io
import logging

from django.core.files.base import ContentFile
from django.db import transaction
from PIL import Image, ImageOps, UnidentifiedImageError

from apps.members import changes
from apps.members.models import Member, MemberPhoto

logger = logging.getLogger(__name__)

FULL, THUMB = 500, 200
MAX_BYTES = 15 * 1024 * 1024
MAX_PIXELS = 50_000_000  # bigger than any phone photo; refuses "decompression bombs"
FORMATS = {"JPEG", "PNG", "WEBP"}


class BadPhoto(ValueError):
    """The message is safe to show the member."""


def _png(image, size) -> bytes:
    square = ImageOps.fit(image, (size, size), Image.Resampling.LANCZOS)  # crops from the middle
    out = io.BytesIO()
    square.save(out, "PNG", optimize=True)
    return out.getvalue()


def process(upload) -> tuple[bytes, bytes]:
    """The 500 px photo and the 200 px thumbnail, from an uploaded file. Raises BadPhoto."""
    if upload.size > MAX_BYTES:
        raise BadPhoto(f"That photo is too big: please send one under {MAX_BYTES // 1024 // 1024} MB.")
    try:
        image = Image.open(upload)
        if image.format not in FORMATS:
            raise BadPhoto("Please send a JPEG, PNG or WebP photo.")
        if image.width * image.height > MAX_PIXELS:
            raise BadPhoto("That photo is too big. Please send a smaller one.")
        image = ImageOps.exif_transpose(image)  # the right way up, as the camera held it
        image = image.convert("RGB")  # also drops any transparency and the camera's other metadata
        return _png(image, FULL), _png(image, THUMB)
    except BadPhoto:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as error:
        logger.info("A photo upload couldn't be read: %s", error)
        raise BadPhoto("That doesn't look like a photo we can read. Please send a JPEG or PNG.") from error


def _store(member, kind, full, thumb) -> MemberPhoto:
    photo = MemberPhoto(member=member, kind=kind)
    photo.image.save("photo.png", ContentFile(full), save=False)
    photo.thumbnail.save("thumb.png", ContentFile(thumb), save=False)
    photo.save()
    return photo


@transaction.atomic
def upload(member, file) -> MemberPhoto:
    """A new photo waiting for approval, replacing one already waiting. Raises BadPhoto."""
    full, thumb = process(file)
    MemberPhoto.objects.filter(member=member, kind=MemberPhoto.Kind.PENDING).delete()  # files go once committed
    # TODO(notifications app): tell the admins there's a photo to approve (06 §6.5).
    return _store(member, MemberPhoto.Kind.PENDING, full, thumb)


@transaction.atomic
def approve(photo: MemberPhoto, by) -> MemberPhoto:
    """The waiting photo becomes the member's photo, replacing the old one."""
    photo = MemberPhoto.objects.select_for_update().get(pk=photo.pk)
    if photo.kind != MemberPhoto.Kind.PENDING:
        return photo
    member = Member.objects.select_for_update().get(pk=photo.member_id)
    MemberPhoto.objects.filter(member=member, kind=MemberPhoto.Kind.CURRENT).delete()
    photo.kind = MemberPhoto.Kind.CURRENT
    photo.save(update_fields=["kind"])
    _set_approved(member, True, by)
    return photo


@transaction.atomic
def decline(photo: MemberPhoto, by, reason=""):
    """The waiting photo is deleted and the member emailed."""
    from apps.billing import emails

    member = photo.member
    deleted, _ = MemberPhoto.objects.filter(pk=photo.pk, kind=MemberPhoto.Kind.PENDING).delete()
    if deleted:
        emails.send(member, "profiles/emails/photo_declined", reason=reason.strip())


@transaction.atomic
def remove(member, kind, by):
    """The member (or an admin) deletes a photo. Without an approved photo, a key holder can't open the door."""
    deleted, _ = MemberPhoto.objects.filter(member=member, kind=kind).delete()
    if deleted and kind == MemberPhoto.Kind.CURRENT:
        _set_approved(Member.objects.select_for_update().get(pk=member.pk), False, by)


def _set_approved(member, value, by):
    if member.photo_approved == value:
        return
    before = changes.snapshot(member)
    member.photo_approved = value
    member.save(update_fields=["photo_approved"])
    changes.record(member, before, by)


# --- who may see what ---

# Members who may see photos marked "only members" (legacy: active, payment-warning and honorary).
SEES_PRIVATE_PHOTOS = Member.STATUSES_WITH_ACCESS


def can_moderate(viewer) -> bool:
    return viewer.is_authenticated and viewer.has_perm("members.change_memberphoto")


def can_see(viewer, member, kind=MemberPhoto.Kind.CURRENT) -> bool:
    """Whether `viewer` may see this member's photo (assuming they may see the member at all)."""
    if viewer.is_authenticated and (viewer.pk == member.pk or can_moderate(viewer)):
        return True
    if kind != MemberPhoto.Kind.CURRENT:
        return False
    if not member.profile.photo_private:
        return True
    return viewer.is_authenticated and viewer.status in SEES_PRIVATE_PHOTOS and not viewer.banned
