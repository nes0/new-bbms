"""Encrypt proofs of identity uploaded before encryption was added (spec 03 §3.1)."""

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.db import migrations


def _fernet():
    return MultiFernet([Fernet(key) for key in settings.ID_DOCUMENT_KEYS]) if settings.ID_DOCUMENT_KEYS else None


def encrypt_existing(apps, schema_editor):
    IdDocument = apps.get_model("members", "IdDocument")
    documents = IdDocument.objects.only("pk", "data")
    if not documents.exists():
        return
    fernet = _fernet()
    if fernet is None:
        raise RuntimeError("Set ID_DOCUMENT_KEYS first: there are proofs of identity to encrypt.")
    for document in documents.iterator(chunk_size=10):
        data = bytes(document.data)
        try:
            fernet.decrypt(data)
            continue  # already encrypted
        except InvalidToken:
            pass
        IdDocument.objects.filter(pk=document.pk).update(data=fernet.encrypt(data))


def decrypt_existing(apps, schema_editor):
    """Going back to code that expects plain files."""
    IdDocument = apps.get_model("members", "IdDocument")
    fernet = _fernet()
    for document in IdDocument.objects.only("pk", "data").iterator(chunk_size=10):
        try:
            data = fernet.decrypt(bytes(document.data)) if fernet else None
        except InvalidToken:
            data = None
        if data is not None:
            IdDocument.objects.filter(pk=document.pk).update(data=data)


class Migration(migrations.Migration):
    dependencies = [("members", "0003_registration_and_id_documents")]

    operations = [migrations.RunPython(encrypt_existing, decrypt_existing, elidable=True)]
