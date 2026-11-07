"""Re-encrypt the proofs of identity with the first key in ID_DOCUMENT_KEYS (spec 03 §3.1).

To change the key: put the new key first in ID_DOCUMENT_KEYS, keeping the old one after it; deploy; run this; then
remove the old key. (Documents are deleted within the time limit in Settings anyway, so waiting that long also works.)
"""

from django.core.management.base import BaseCommand, CommandError

from apps.members import id_documents


class Command(BaseCommand):
    help = "Re-encrypt every stored proof of identity with the first key in ID_DOCUMENT_KEYS."

    def handle(self, *args, **options):
        if not id_documents.encryption_ready():
            raise CommandError("ID_DOCUMENT_KEYS isn't set.")
        try:
            count = id_documents.rotate_keys()
        except id_documents.CannotDecrypt as error:
            raise CommandError(f"No key in ID_DOCUMENT_KEYS opens {error}. Nothing after it was changed.") from error
        self.stdout.write(f"Re-encrypted {count} document(s) with the first key.")
