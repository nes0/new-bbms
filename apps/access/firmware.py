"""
Firmware releases for the readers, sent to them over the network (spec 05 §5.4, docs/device-api-v2.md).

A release is a zip built by the firmware repository's make_release.py: the reader's files, and a manifest.json
giving the version and each file's SHA-256. An admin uploads it and chooses which readers should run it; each
reader is told at its next check-in, downloads the files that differ, and restarts into them.
"""

import hashlib
import json
import re
import zipfile

from django.db import transaction

from .models import Device, FirmwareFile, FirmwareRelease

MAX_BUNDLE_BYTES = 2 * 1024 * 1024
MAX_FILES = 100
MAX_FILE_BYTES = 256 * 1024  # the reader refuses anything bigger
PATH = re.compile(r"[a-z0-9_]+(/[a-z0-9_]+)*\.(py|mpy)")
VERSION = re.compile(r"[A-Za-z0-9._-]{1,50}")
# The reader refuses these too: its loader, MicroPython's start-up file, and its own settings and update folders.
PROTECTED = ("main.py", "boot.py", "data/", "ota/")


class FirmwareError(Exception):
    """Something wrong with an uploaded release, worded for the admin who uploaded it."""


def read_bundle(upload) -> tuple[str, dict[str, bytes]]:
    """(version, {path: contents}) from an uploaded release zip. Raises FirmwareError."""
    if upload.size > MAX_BUNDLE_BYTES:
        raise FirmwareError("That file is too big to be a firmware release.")
    try:
        bundle = zipfile.ZipFile(upload)
        names = [info for info in bundle.infolist() if not info.is_dir()]
        if sum(info.file_size for info in names) > MAX_BUNDLE_BYTES:
            raise FirmwareError("That file is too big to be a firmware release.")
        contents = {info.filename: bundle.read(info) for info in names}
    except (zipfile.BadZipFile, OSError, NotImplementedError):
        raise FirmwareError("That isn't a zip file. Upload the zip that make_release.py makes.") from None

    try:
        manifest = json.loads(contents.pop("manifest.json"))
        version, hashes = manifest["version"], manifest["files"]
        if not isinstance(version, str) or not isinstance(hashes, dict):
            raise TypeError
    except (KeyError, ValueError, TypeError):
        raise FirmwareError("The zip has no usable manifest.json. Upload the zip that make_release.py makes.") from None

    if not VERSION.fullmatch(version):
        raise FirmwareError("The version may only have letters, digits, dots, hyphens and underscores (50 at most).")
    if not contents or len(contents) > MAX_FILES:
        raise FirmwareError(f"A release needs between 1 and {MAX_FILES} files.")
    if set(contents) != set(hashes):
        raise FirmwareError("The files in the zip aren't the ones its manifest lists.")
    for path, data in contents.items():
        if not PATH.fullmatch(path) or path.startswith(PROTECTED):
            raise FirmwareError(f"“{path}” isn't a file a release may contain.")
        if len(data) > MAX_FILE_BYTES:
            raise FirmwareError(f"“{path}” is too big for a reader.")
        if hashlib.sha256(data).hexdigest() != hashes[path]:
            raise FirmwareError(f"“{path}” doesn't match its checksum in the manifest: the zip is damaged.")
    return version, contents


@transaction.atomic
def create_release(version: str, contents: dict[str, bytes], *, notes="", uploaded_by=None) -> FirmwareRelease:
    if FirmwareRelease.objects.filter(version=version).exists():
        raise FirmwareError(
            f"Version {version} has been uploaded already. Change VERSION in the firmware's version.py and build "
            "the release again: readers tell versions apart by that name."
        )
    release = FirmwareRelease.objects.create(version=version, notes=notes, uploaded_by=uploaded_by)
    FirmwareFile.objects.bulk_create(
        FirmwareFile(release=release, path=path, sha256=hashlib.sha256(data).hexdigest(), size=len(data), content=data)
        for path, data in sorted(contents.items())
    )
    return release


def manifest(release: FirmwareRelease) -> dict:
    files = release.files.order_by("path").values("path", "size", "sha256")
    return {"version": release.version, "files": list(files)}


def set_target(device: Device, release: FirmwareRelease | None):
    """Choose the firmware a reader should run (None: leave it as it is)."""
    if device.target_firmware_id != (release.pk if release else None):
        device.target_firmware = release
        device.firmware_update_error = ""
        device.firmware_update_error_at = None
        device.save(update_fields=["target_firmware", "firmware_update_error", "firmware_update_error_at"])


def status(device: Device) -> str:
    """Where a reader's firmware update has got to, for the admin."""
    target = device.target_firmware
    if target is None:
        return ""
    if device.firmware_version == target.version:
        return "up to date"
    if device.firmware_update_error:
        return f"update to {target.version} failed: {device.firmware_update_error}"
    return f"waiting to update to {target.version}"
