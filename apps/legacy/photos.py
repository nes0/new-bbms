"""
Fetching members' photos from the legacy system's S3 bucket (spec 08 §8.2, "S3 photos").

Legacy keeps each member's photo as public PNGs named after the md5 of their `users.hash` (a random string, not the
id), under
`<bucket>/<environment>/user-photo/`: `<md5>.png` (500 px) and `<md5>-thumb.png` (200 px) for the approved photo,
and `-new.png` / `-thumb-new.png` for one waiting for approval. The import is given that folder's address, e.g.
`https://s3-eu-west-1.amazonaws.com/<bucket>/production`, and copies what it finds into private storage under random
names.
"""

import hashlib
import urllib.error
import urllib.request

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_BYTES = 5 * 1024 * 1024  # legacy photos are at most 500 x 500 px
RECEIPT_MAX_BYTES = 15 * 1024 * 1024
RECEIPT_SIGNATURES = (b"%PDF-", PNG_SIGNATURE, b"\xff\xd8\xff")
TIMEOUT_SECONDS = 20

# kind -> (photo suffix, thumbnail suffix)
NAMES = {"current": (".png", "-thumb.png"), "pending": ("-new.png", "-thumb-new.png")}


class PhotoSource:
    """Downloads legacy photos. `save=False` (a dry run) only checks they're there."""

    def __init__(self, base_url, save=True, opener=None):
        base_url = base_url.rstrip("/")
        if not base_url.startswith("https://"):
            raise ValueError("The photos' address must start with https://")
        self.base_url = base_url
        self.save = save
        self._open = opener or self._download

    @staticmethod
    def _download(url):
        request = urllib.request.Request(url, headers={"User-Agent": "BBMS legacy import"})
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 (https only)
            return response.read(RECEIPT_MAX_BYTES + 1)

    def fetch(self, user_hash, kind):
        """(photo, thumbnail) as PNG bytes, or None if either is missing or isn't a PNG. `user_hash` is `users.hash`."""
        stem = hashlib.md5(user_hash.encode()).hexdigest()  # noqa: S324 (legacy's naming, not security)
        found = []
        for suffix in NAMES[kind]:
            try:
                data = self._open(f"{self.base_url}/user-photo/{stem}{suffix}")
            except (urllib.error.URLError, TimeoutError, OSError):
                return None
            if not data or len(data) > MAX_BYTES or not data.startswith(PNG_SIGNATURE):
                return None
            found.append(data)
        return tuple(found)

    def fetch_receipt(self, path):
        """An expense receipt (PDF, PNG or JPEG bytes), or None. `path` is legacy's `expenses.file`, e.g.
        "production/expenses/<random>.pdf": it's found next to the photos' folder, in the bucket's root.
        """
        root = self.base_url.rsplit("/", 1)[0]
        if not path or ".." in path or path.startswith("/"):
            return None
        try:
            data = self._open(f"{root}/{path}")
        except (urllib.error.URLError, TimeoutError, OSError):
            return None
        if not data or len(data) > RECEIPT_MAX_BYTES or not data.startswith(RECEIPT_SIGNATURES):
            return None
        return data
