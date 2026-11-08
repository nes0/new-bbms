"""Startup checks for the members app."""

from cryptography.fernet import Fernet
from django.conf import settings
from django.core import checks


@checks.register(checks.Tags.security)
def id_document_keys_are_valid(app_configs, **kwargs):
    """Every key in ID_DOCUMENT_KEYS must be a real Fernet key, or nothing could be encrypted or opened."""
    errors = []
    for position, key in enumerate(settings.ID_DOCUMENT_KEYS, start=1):
        try:
            Fernet(key)
        except (ValueError, TypeError):
            errors.append(
                checks.Error(
                    f"Key {position} in ID_DOCUMENT_KEYS isn't a Fernet key.",
                    hint='Make one with: python -c "from cryptography.fernet import Fernet; '
                    'print(Fernet.generate_key().decode())"',
                    id="members.E001",
                )
            )
    return errors


@checks.register(checks.Tags.security, deploy=True)
def id_document_keys_are_set(app_configs, **kwargs):
    """Without a key, members can't upload proofs of identity (they're never stored unencrypted)."""
    if settings.ID_DOCUMENT_KEYS:
        return []
    return [
        checks.Warning(
            "ID_DOCUMENT_KEYS isn't set, so members can't upload proofs of identity.",
            hint="Set it in the app's environment (see bbms/settings.py and spec 03 §3.1).",
            id="members.W001",
        )
    ]


@checks.register(checks.Tags.security, deploy=True)
def photo_storage_is_set(app_configs, **kwargs):
    """Without Spaces, uploaded photos go on the app's own disk, which App Platform wipes on every deploy."""
    if settings.SPACES_BUCKET:
        return []
    return [
        checks.Warning(
            "SPACES_BUCKET isn't set, so member photos are kept on the app's disk and lost when it's redeployed.",
            hint="Set SPACES_BUCKET, SPACES_KEY and SPACES_SECRET (see bbms/settings.py and spec 01 §1.8).",
            id="members.W002",
        )
    ]
