"""
What a screen by the door may show about who's in the space (spec 05 §5.4, "Occupancy screen"; 06 §6.7).

The list is the same estimate as the "Who's in" page (`activity.in_space`), most recent arrival first. The screen sits
in the space, where visitors can see it too, so it follows what a visitor who isn't logged in may see:
- a member with a **private profile** is counted but shown without a name, photo or id;
- a member who chose **"only members see my photo"** is shown by name, without a photo;
- names are `display_name` ("Kim K."), like everything else a device shows.

Photos are only served for members who are in the space now, so a device key can't be used to collect everyone's.
They're made from the member's approved 200 px thumbnail, scaled to the size the screen asks for.
"""

import hashlib
import io
import struct

from django.core.exceptions import ObjectDoesNotExist
from PIL import Image

from apps.members.models import MemberPhoto

from . import activity

DEFAULT_SIZE, MIN_SIZE, MAX_SIZE = 64, 16, 200
FORMATS = {"rgb565": "application/octet-stream", "png": "image/png", "jpeg": "image/jpeg"}


def _photo_private(member) -> bool:
    try:
        return member.profile.photo_private
    except ObjectDoesNotExist:
        return False


def shown_photo(member) -> MemberPhoto | None:
    """The approved photo a door screen may show for this member, or None."""
    if member.profile_private or _photo_private(member) or not member.photo_approved:
        return None
    return member.photos.filter(kind=MemberPhoto.Kind.CURRENT).first()


def photo_version(photo: MemberPhoto) -> str:
    """Changes when the member's photo changes (a new photo is stored under a new random name)."""
    return hashlib.sha256(photo.thumbnail.name.encode()).hexdigest()[:8]


def people(now=None) -> list[dict]:
    """Who's in, as the screen may show them."""
    found = []
    for presence in activity.in_space(now):
        member = presence.member
        since = int(presence.since.timestamp()) if presence.since else None
        if member.profile_private:
            found.append({"id": None, "name": None, "since": since, "using": [], "photo": None})
            continue
        photo = shown_photo(member)
        found.append(
            {
                "id": member.pk,
                "name": member.display_name,
                "since": since,
                "using": sorted(presence.using),
                "photo": photo_version(photo) if photo else None,
            }
        )
    return found


def version(entries) -> str:
    """First 8 hex characters of SHA-256 over the list: changes when anything on the screen would."""
    text = "\n".join(f"{e['id']}|{e['name']}|{e['since']}|{','.join(e['using'])}|{e['photo']}" for e in entries)
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def is_in(member, now=None) -> bool:
    return any(presence.member.pk == member.pk for presence in activity.in_space(now))


def rgb565(image: Image.Image) -> bytes:
    """Width and height (2 bytes each, little-endian), then each pixel as RGB565, little-endian, row by row."""
    pixels = image.convert("RGB").getdata()
    packed = bytearray(struct.pack("<HH", image.width, image.height))
    for red, green, blue in pixels:
        packed += struct.pack("<H", (red >> 3) << 11 | (green >> 2) << 5 | blue >> 3)
    return bytes(packed)


def render(photo: MemberPhoto, size: int, fmt: str) -> bytes:
    """The member's thumbnail as a `size` × `size` square in one of FORMATS."""
    with photo.thumbnail.open("rb") as file:
        image = Image.open(file).convert("RGB")
        image.load()
    if image.size != (size, size):
        image = image.resize((size, size), Image.Resampling.LANCZOS)
    if fmt == "rgb565":
        return rgb565(image)
    out = io.BytesIO()
    if fmt == "png":
        image.save(out, "PNG", optimize=True)
    else:
        image.save(out, "JPEG", quality=85)  # baseline JPEG, which microcontroller decoders handle
    return out.getvalue()
