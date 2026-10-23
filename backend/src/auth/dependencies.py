"""FastAPI dependencies for database-backed authentication and role checks."""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from src.auth.jwt_codec import InvalidAccessToken, JwtCodec
from src.auth.session_service import ActiveIdentity, InvalidSession, validate_session
from src.core.database import get_session

RoleCode = Literal["admin", "manager", "employee"]
SessionDependency = Annotated[Session, Depends(get_session)]
bearer_scheme = HTTPBearer(
    scheme_name="BearerAuth", bearerFormat="JWT", auto_error=False
)


class AuthHttpError(Exception):
    """A safe problem response selected by the HTTP adapter."""

    def __init__(
        self,
        status: int,
        kind: str,
        title: str,
        detail: str,
        *,
        bearer: bool = False,
    ) -> None:
        self.status = status
        self.kind = kind
        self.title = title
        self.detail = detail
        self.bearer = bearer


def require_codec(request: Request) -> JwtCodec:
    """Require signing configuration without exposing configuration details."""
    codec = request.app.state.jwt_codec
    if codec is None:
        raise AuthHttpError(
            503,
            "about:blank",
            "Usługa niedostępna",
            "Uwierzytelnianie jest niedostępne.",
        )
    return codec


def require_origin(request: Request) -> None:
    """Require a trusted origin for operations involving a refresh cookie."""
    settings = request.app.state.settings
    origin = request.headers.get("origin")
    if origin is None or origin not in settings.cors_origins:
        raise AuthHttpError(
            403, "about:blank", "Odmowa dostępu", "Niedozwolone żądanie."
        )
    require_https(request)


def require_https(request: Request) -> None:
    """Reject unencrypted requests outside the approved local environment."""
    if (
        request.app.state.settings.environment == "production"
        and request.url.scheme != "https"
    ):
        raise AuthHttpError(
            403, "about:blank", "Odmowa dostępu", "Niedozwolone żądanie."
        )


def _unauthenticated() -> AuthHttpError:
    """Build the existing bearer challenge and safe problem detail."""
    return AuthHttpError(
        401,
        "about:blank",
        "Brak uwierzytelnienia",
        "Nieprawidłowe uwierzytelnienie.",
        bearer=True,
    )


def get_current_identity(
    request: Request,
    session: SessionDependency,
    _credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
) -> ActiveIdentity:
    """Validate Bearer JWT and return current account state from PostgreSQL."""
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token or " " in token:
        raise _unauthenticated()
    try:
        user_id, session_id = require_codec(request).verify(token)
        return validate_session(session, session_id, user_id)
    except (InvalidAccessToken, InvalidSession):
        raise _unauthenticated() from None


CurrentIdentity = Annotated[ActiveIdentity, Depends(get_current_identity)]


def require_roles(*allowed_roles: RoleCode) -> Callable[..., ActiveIdentity]:
    """Require one or more explicit roles from the current database identity.

    Args:
        allowed_roles: Role codes accepted by the protected operation.

    Returns:
        A FastAPI dependency that returns the authorized identity.

    Raises:
        ValueError: If no role is specified.
    """
    if not allowed_roles:
        raise ValueError("At least one role is required")
    if set(allowed_roles) - {"admin", "manager", "employee"}:
        raise ValueError("Unsupported role")

    def check_role(identity: CurrentIdentity) -> ActiveIdentity:
        """Reject authenticated callers without an explicitly allowed role."""
        if identity.role not in allowed_roles:
            raise AuthHttpError(
                403, "about:blank", "Odmowa dostępu", "Brak uprawnienia."
            )
        return identity

    return check_role
