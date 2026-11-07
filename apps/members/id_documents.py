"""
Proof of identity for new members: what may be uploaded, and shredding it afterwards (spec 03 §3.1).

The rules in one place:
- PDF, JPEG, PNG, WebP or HEIC (iPhone photos), recognised from the file's contents, not its name;
- at most MAX_FILE_BYTES each;
- uploads are held in memory while the request is handled, never in a temporary file on disk;
- deleted as soon as a trustee has checked them, or after MAX_AGE if nobody does.
"""

import re
from datetime import timedelta

from django.core.files.uploadhandler import MemoryFileUploadHandler
from django.db import transaction
from django.utils import timezone

from .models import IdCheck, IdDocument

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_REQUEST_BYTES = 2 * MAX_FILE_BYTES + 64 * 1024  # two files plus the form fields
# TODO(settings app): how long unchecked uploads are kept is a setting (02 §2.8); suggested 30 days.
MAX_AGE = timedelta(days=30)

# Types a browser can show safely in a tab; anything else is downloaded instead.
SHOWN_INLINE = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}


def sniff_content_type(data: bytes) -> str | None:
    """The file's real type from its first bytes, or None if it isn't one we accept."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
        return "image/heic"
    return None


def safe_filename(name: str) -> str:
    """Keep a readable name for the trustee, without anything that could upset a download header."""
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", (name or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]).strip(" .")
    return (name or "document")[:100]


class InMemoryUploadHandler(MemoryFileUploadHandler):
    """Hold ID uploads in memory only: Django normally writes uploads over 2.5 MB to a temporary file.

    The view refuses requests over MAX_REQUEST_BYTES before parsing, so memory use is bounded.
    """

    def handle_raw_input(self, input_data, META, content_length, boundary, encoding=None):
        self.activated = True


@transaction.atomic
def save_document(member, kind: str, upload, data: bytes, content_type: str) -> IdDocument:
    """Store (or replace) the member's document of this kind."""
    IdDocument.objects.filter(member=member, kind=kind).delete()
    return IdDocument.objects.create(
        member=member,
        kind=kind,
        filename=safe_filename(upload.name),
        content_type=content_type,
        size=len(data),
        data=data,
    )


@transaction.atomic
def record_check(member, reviewer, result: str, note: str = "") -> IdCheck:
    """Record a trustee's decision and shred the documents, whatever the decision."""
    documents = IdDocument.objects.select_for_update().filter(member=member)
    seen = ", ".join(sorted(documents.values_list("kind", flat=True)))
    check = IdCheck.objects.create(member=member, result=result, note=note, documents_seen=seen, checked_by=reviewer)
    documents.delete()
    # TODO(notifications app): email the member: approved, or "please send again" with the note.
    return check


def shred_stale(now=None) -> int:
    """Delete uploads nobody has checked within MAX_AGE. Returns how many were deleted."""
    now = now or timezone.now()
    deleted, _ = IdDocument.objects.filter(uploaded_at__lt=now - MAX_AGE).delete()
    return deleted


def status(member) -> str:
    """'approved', 'waiting' (uploaded, not yet checked), 'rejected' (asked to send again) or 'needed'."""
    if member.id_documents.exists():
        return "waiting"
    latest = member.id_checks.first()
    if latest is None:
        return "needed"
    return latest.result
