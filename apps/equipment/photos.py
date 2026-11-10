"""
Photos of equipment (spec 05 §5.7): the equipment team uploads them in the admin, members see them on the machine's
page. As legacy, each is cropped to a square from the middle; here it's a 1000 px JPEG with a 300 px thumbnail.
They're in private storage and served through BBMS to those who may see the machine's page.
"""

import io
import logging

from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

from .models import EquipmentPhoto

logger = logging.getLogger(__name__)

FULL, THUMB = 1000, 300
MAX_BYTES = 15 * 1024 * 1024
MAX_PIXELS = 50_000_000  # bigger than any phone photo; refuses "decompression bombs"
FORMATS = {"JPEG", "PNG", "WEBP"}


class BadPhoto(ValueError):
    """The message is safe to show."""


def _jpeg(image, size) -> bytes:
    square = ImageOps.fit(image, (size, size), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    square.save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()


def process(file) -> tuple[bytes, bytes]:
    """The photo and its thumbnail, from an uploaded file or a file-like object. Raises BadPhoto."""
    size = getattr(file, "size", None)
    if size is not None and size > MAX_BYTES:
        raise BadPhoto(f"That photo is too big: please send one under {MAX_BYTES // 1024 // 1024} MB.")
    try:
        image = Image.open(file)
        if image.format not in FORMATS:
            raise BadPhoto("Please send a JPEG, PNG or WebP photo.")
        if image.width * image.height > MAX_PIXELS:
            raise BadPhoto("That photo is too big. Please send a smaller one.")
        image = ImageOps.exif_transpose(image).convert("RGB")  # the right way up; drops the camera's metadata
        return _jpeg(image, FULL), _jpeg(image, THUMB)
    except BadPhoto:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as error:
        logger.info("An equipment photo couldn't be read: %s", error)
        raise BadPhoto("That doesn't look like a photo we can read. Please send a JPEG or PNG.") from error


def fill(photo: EquipmentPhoto, full: bytes, thumb: bytes) -> EquipmentPhoto:
    """Put processed images into a photo (not saved)."""
    photo.image.save("photo.jpg", ContentFile(full), save=False)
    photo.thumbnail.save("thumb.jpg", ContentFile(thumb), save=False)
    return photo


def add(equipment, file, *, position=None) -> EquipmentPhoto:
    """A new photo of this machine, after the others unless a position is given. Raises BadPhoto."""
    full, thumb = process(file)
    if position is None:
        last = equipment.photos.order_by("-position").values_list("position", flat=True).first()
        position = 0 if last is None else last + 1
    photo = fill(EquipmentPhoto(equipment=equipment, position=position), full, thumb)
    photo.save()
    return photo
