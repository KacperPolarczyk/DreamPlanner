"""Password hashing and verification for authentication workflows."""

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

# Initial OWASP Argon2id minimum approved in AUTH_DESIGN.md.
_hasher = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


def hash_password(password: str) -> str:
    """Hash a password with a fresh random salt.

    Args:
        password: The password to hash.

    Returns:
        An encoded Argon2id hash.
    """
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Check a password against an encoded Argon2id hash.

    Args:
        password: The candidate password.
        password_hash: The stored encoded hash.

    Returns:
        Whether the password matches a valid hash.
    """
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False
