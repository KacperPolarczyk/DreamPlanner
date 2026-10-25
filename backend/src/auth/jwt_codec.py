"""Strict HS256 access-token signing and verification."""

import base64
import binascii
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
from jwt.exceptions import InvalidTokenError

from src.core.config import Settings

ACCESS_LIFETIME = timedelta(minutes=10)
_REQUIRED_CLAIMS = ["sub", "iss", "aud", "iat", "nbf", "exp", "sid", "token_use"]
_MAX_USER_ID = 2**63 - 1


class InvalidAccessToken(Exception):
    """An access token fails signature or claim validation."""


class JwtConfigurationError(ValueError):
    """JWT signing configuration is absent or unsafe."""


def _decode_key(value: str | None) -> bytes:
    """Decode a base64 key with at least 256 bits of supplied key material."""
    if not value:
        raise JwtConfigurationError("JWT signing key is not configured")
    try:
        key = base64.b64decode(value, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise JwtConfigurationError("JWT signing key is invalid") from None
    if len(key) < 32:
        raise JwtConfigurationError("JWT signing key is too short")
    return key


class JwtCodec:
    """Issue access tokens and accept only locally configured key IDs."""

    def __init__(self, settings: Settings) -> None:
        if not settings.jwt_issuer or not settings.jwt_audience:
            raise JwtConfigurationError("JWT issuer and audience are required")
        if not settings.jwt_current_kid:
            raise JwtConfigurationError("Current JWT key ID is required")
        current = _decode_key(
            settings.jwt_signing_key_base64.get_secret_value()
            if settings.jwt_signing_key_base64
            else None
        )
        self.current_kid = settings.jwt_current_kid
        self.keys = {self.current_kid: current}
        if settings.jwt_previous_signing_key_base64 is not None:
            if (
                not settings.jwt_previous_kid
                or settings.jwt_previous_kid == self.current_kid
            ):
                raise JwtConfigurationError("Previous JWT key ID must be distinct")
            self.keys[settings.jwt_previous_kid] = _decode_key(
                settings.jwt_previous_signing_key_base64.get_secret_value()
            )
        elif settings.jwt_previous_kid:
            raise JwtConfigurationError("Previous JWT key is required for its ID")
        self.issuer = settings.jwt_issuer
        self.audience = settings.jwt_audience

    def issue(self, user_id: int, session_id: UUID) -> str:
        """Sign a ten-minute access token for a pending or committed session."""
        now = int(datetime.now(UTC).timestamp())
        payload = {
            "sub": str(user_id),
            "iss": self.issuer,
            "aud": self.audience,
            "iat": now,
            "nbf": now,
            "exp": now + int(ACCESS_LIFETIME.total_seconds()),
            "sid": str(session_id),
            "token_use": "access",
            "jti": str(uuid4()),
        }
        return jwt.encode(
            payload,
            self.keys[self.current_kid],
            algorithm="HS256",
            headers={"kid": self.current_kid},
        )

    def verify(self, token: str) -> tuple[int, UUID]:
        """Validate signature, fixed algorithm, all claims and token lifetime.

        Raises:
            InvalidAccessToken: If verification fails for any reason.
        """
        try:
            if len(token) > 4096:
                raise InvalidAccessToken("Invalid access token.")
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != "HS256" or not isinstance(kid, str):
                raise InvalidAccessToken("Invalid access token.")
            key = self.keys.get(kid)
            if key is None:
                raise InvalidAccessToken("Invalid access token.")
            claims = jwt.decode(
                token,
                key,
                algorithms=["HS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": _REQUIRED_CLAIMS, "strict_aud": True},
            )
            now = int(datetime.now(UTC).timestamp())
            times = (claims["iat"], claims["nbf"], claims["exp"])
            if not all(type(value) is int for value in times):
                raise InvalidAccessToken("Invalid access token.")
            issued, not_before, expires = times
            if (
                issued > now
                or not_before > expires
                or expires <= issued
                or expires - issued > int(ACCESS_LIFETIME.total_seconds())
                or claims["token_use"] != "access"
            ):
                raise InvalidAccessToken("Invalid access token.")
            subject = claims["sub"]
            if (
                not isinstance(subject, str)
                or len(subject) > 19
                or not subject.isascii()
                or not subject.isdecimal()
                or str(int(subject)) != subject
            ):
                raise InvalidAccessToken("Invalid access token.")
            user_id = int(subject)
            if not 0 < user_id <= _MAX_USER_ID:
                raise InvalidAccessToken("Invalid access token.")
            sid = claims["sid"]
            if not isinstance(sid, str):
                raise InvalidAccessToken("Invalid access token.")
            return user_id, UUID(sid)
        except (InvalidTokenError, ValueError, TypeError, KeyError):
            raise InvalidAccessToken("Invalid access token.") from None
