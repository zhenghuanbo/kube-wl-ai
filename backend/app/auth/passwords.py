"""Password hashing.

Uses argon2-cffi directly rather than passlib: passlib 1.7.4 is unmaintained and
depends on the stdlib ``crypt`` module that Python 3.13 removed.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

#: Tuned for an interactive login endpoint: ~50ms per verification on
#: commodity hardware while staying well inside OWASP's recommendations.
_hasher = PasswordHasher(
    time_cost=2,
    memory_cost=64 * 1024,  # 64 MiB
    parallelism=2,
    hash_len=32,
    salt_len=16,
)

#: Minimum length accepted for a locally managed password.
MIN_PASSWORD_LENGTH = 12


def hash_password(plain: str) -> str:
    """Return an argon2id hash of ``plain``."""
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str | None) -> bool:
    """Constant-time verification; returns False for any malformed hash."""
    if not hashed:
        return False
    try:
        return _hasher.verify(hashed, plain)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    """Whether the stored hash was produced with outdated parameters."""
    try:
        return _hasher.check_needs_rehash(hashed)
    except InvalidHashError:
        return True
