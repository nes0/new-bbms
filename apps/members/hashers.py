"""
Password hasher for passwords migrated from the legacy (Laravel/PHP) system.

Legacy passwords are bcrypt hashes such as `$2y$10$...`. The migration stores them as
`legacy_bcrypt$<the original hash>`. When a member logs in with one, Django checks it here and
then re-hashes the password with the current default hasher, so legacy hashes disappear over time.

PHP's bcrypt only uses the first 72 bytes of a password and silently ignores the rest. The Python
`bcrypt` library (version 5+) refuses longer passwords instead, so we cut them to 72 bytes here
to give exactly the same answer as the legacy system.
"""

import bcrypt
from django.contrib.auth.hashers import BasePasswordHasher, mask_hash

BCRYPT_MAX_BYTES = 72


def _bcrypt_input(password: str) -> bytes:
    return password.encode("utf-8")[:BCRYPT_MAX_BYTES]


def from_legacy_hash(legacy_hash: str) -> str:
    """Turn a legacy `$2y$...` hash into a value for `Member.password`."""
    return f"{LegacyBcryptPasswordHasher.algorithm}${legacy_hash}"


class LegacyBcryptPasswordHasher(BasePasswordHasher):
    algorithm = "legacy_bcrypt"

    def salt(self):
        return bcrypt.gensalt(rounds=10).decode("ascii")

    def encode(self, password, salt):
        # Only used by tests and by Django internals; real members are always upgraded on login.
        hashed = bcrypt.hashpw(_bcrypt_input(password), salt.encode("ascii")).decode("ascii")
        return f"{self.algorithm}${hashed}"

    def decode(self, encoded):
        algorithm, legacy_hash = encoded.split("$", 1)
        assert algorithm == self.algorithm
        return {"algorithm": algorithm, "hash": legacy_hash, "salt": legacy_hash[:29]}

    def verify(self, password, encoded):
        legacy_hash = self.decode(encoded)["hash"]
        try:
            # checkpw compares in constant time and understands PHP's $2y$ prefix.
            return bcrypt.checkpw(_bcrypt_input(password), legacy_hash.encode("ascii"))
        except ValueError:
            return False  # not a valid bcrypt hash

    def safe_summary(self, encoded):
        decoded = self.decode(encoded)
        return {"algorithm": decoded["algorithm"], "hash": mask_hash(decoded["hash"])}

    def must_update(self, encoded):
        # Always move members off legacy hashes as soon as they log in.
        return True

    def harden_runtime(self, password, encoded):
        pass
