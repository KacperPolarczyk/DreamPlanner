"""Opaque activation token generation and one-way storage hashing."""

import hashlib
import secrets


def generate_action_token() -> str:
    """Generate a URL-safe token containing 256 random bits."""
    return secrets.token_urlsafe(32)


def hash_action_token(token: str) -> bytes:
    """Return the SHA-256 digest stored for an opaque random token."""
    return hashlib.sha256(token.encode("utf-8")).digest()


def generate_refresh_token() -> str:
    """Generate a URL-safe opaque token with 256 random bits."""
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> bytes:
    """Return the digest stored for a refresh token."""
    return hashlib.sha256(token.encode("utf-8")).digest()
