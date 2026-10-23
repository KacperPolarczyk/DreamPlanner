"""HTTP boundary for login, refresh, logout, and first activation."""

from datetime import UTC, datetime
from typing import Annotated

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from src.auth.dependencies import (
    AuthHttpError,
    CurrentIdentity,
    require_codec,
)
from src.auth.rate_limits import AuthRateLimits, LimitStatus
from src.auth.repository import ActionTokenRepository
from src.auth.schemas import AccessTokenResponse, LoginInput, ProblemDetails
from src.auth.session_repository import AuthSessionRepository
from src.auth.session_service import (
    InvalidCredentials,
    InvalidSession,
    SessionGrant,
    login,
    refresh_session,
    revoke_session,
)
from src.auth.tokens import hash_action_token, hash_refresh_token
from src.core.config import Settings
from src.core.database import get_session
from src.users.account_service import InvalidActionToken, activate_account
from src.users.schemas import ActivateAccountInput
from src.users.validation import AccountValidationError

router = APIRouter(prefix="/auth", tags=["auth"])
SessionDependency = Annotated[Session, Depends(get_session)]


def _problem_response(description: str) -> dict[str, object]:
    """Document the custom problem media type in OpenAPI."""
    return {
        "description": description,
        "content": {
            "application/problem+json": {"schema": ProblemDetails.model_json_schema()}
        },
    }


def _settings(request: Request) -> Settings:
    """Read the application's validated environment settings."""
    return request.app.state.settings


def _limiter(request: Request) -> AuthRateLimits:
    """Return this FastAPI application's isolated SlowAPI counters."""
    return request.app.state.auth_rate_limits


def _require_origin(request: Request) -> None:
    """Reject missing or unlisted browser origins before state changes."""
    settings = _settings(request)
    origin = request.headers.get("origin")
    if (
        origin is None
        or origin not in settings.cors_origins
        or (settings.environment == "production" and request.url.scheme != "https")
    ):
        raise AuthHttpError(
            403, "about:blank", "Odmowa dostępu", "Niedozwolone żądanie."
        )


def _login_identity(email: str) -> str:
    """Normalize an email for limiting without disclosing account existence."""
    try:
        normalized = validate_email(
            email.strip(), check_deliverability=False
        ).normalized
    except EmailNotValidError:
        normalized = email.strip().lower()
    return f"email:{normalized.lower()}"


def _token_identity(prefix: str, digest: bytes, known_id: str | None) -> str:
    """Use one counter per known subject or opaque token fingerprint."""
    return f"{prefix}:{known_id}" if known_id else f"unknown:{digest.hex()}"


def _grant_response(
    request: Request, grant: SessionGrant, limit_status: LimitStatus
) -> JSONResponse:
    """Send only the access JWT in JSON and the refresh value in a cookie."""
    settings = _settings(request)
    payload = AccessTokenResponse(access_token=grant.access_token)
    response = JSONResponse(payload.model_dump(), headers=limit_status.headers())
    response.headers["Cache-Control"] = "no-store"
    max_age = max(
        1, int((grant.refresh_expires_at - datetime.now(UTC)).total_seconds())
    )
    response.set_cookie(
        settings.refresh_cookie_name,
        grant.refresh_token,
        max_age=max_age,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite=settings.refresh_cookie_samesite,
        path=settings.refresh_cookie_path,
    )
    return response


@router.post(
    "/sessions",
    response_model=AccessTokenResponse,
    responses={
        400: _problem_response("Invalid JSON"),
        401: _problem_response("invalid-credentials"),
        403: _problem_response("Origin denied"),
        422: _problem_response("validation-error"),
        429: _problem_response("rate-limited"),
    },
)
def create_session(
    request: Request, data: LoginInput, session: SessionDependency
) -> Response:
    """Create one session after both limits and credential checks."""
    limit_status = _limiter(request).check_identity(
        request, "login", _login_identity(data.email)
    )
    _require_origin(request)
    try:
        grant = login(
            session,
            data.email,
            data.password.get_secret_value(),
            require_codec(request),
        )
    except InvalidCredentials:
        raise AuthHttpError(
            401,
            "urn:dreamplanner:problem:invalid-credentials",
            "Nie udało się zalogować",
            "Nieprawidłowe dane logowania.",
        ) from None
    return _grant_response(request, grant, limit_status)


@router.post(
    "/sessions/current/refresh",
    response_model=AccessTokenResponse,
    responses={
        401: _problem_response("invalid-session"),
        403: _problem_response("Origin denied"),
        429: _problem_response("rate-limited"),
    },
)
def renew_session(request: Request, session: SessionDependency) -> Response:
    """Rotate a cookie-held refresh token and issue a new access JWT."""
    token = request.cookies.get(_settings(request).refresh_cookie_name, "")
    digest = hash_refresh_token(token)
    known_id = AuthSessionRepository(session).find_token_session_id(digest)
    key = _token_identity("session", digest, str(known_id) if known_id else None)
    limit_status = _limiter(request).check_identity(request, "refresh", key)
    _require_origin(request)
    try:
        grant = refresh_session(session, token, require_codec(request))
    except InvalidSession:
        raise AuthHttpError(
            401,
            "urn:dreamplanner:problem:invalid-session",
            "Sesja jest nieważna",
            "Nieprawidłowa sesja.",
        ) from None
    return _grant_response(request, grant, limit_status)


@router.delete(
    "/sessions/current",
    status_code=204,
    dependencies=[Depends(_require_origin)],
    responses={
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Origin denied"),
    },
)
def delete_session(
    request: Request, session: SessionDependency, identity: CurrentIdentity
) -> Response:
    """Revoke the access token's current session and remove its cookie."""
    try:
        revoke_session(session, identity.session_id, identity.user_id)
    except InvalidSession:
        raise AuthHttpError(
            401,
            "about:blank",
            "Brak uwierzytelnienia",
            "Nieprawidłowe uwierzytelnienie.",
            bearer=True,
        ) from None
    settings = _settings(request)
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.delete_cookie(
        settings.refresh_cookie_name,
        path=settings.refresh_cookie_path,
        secure=settings.refresh_cookie_secure,
        httponly=True,
        samesite=settings.refresh_cookie_samesite,
    )
    return response


@router.post(
    "/activations",
    status_code=204,
    responses={
        400: _problem_response("Invalid JSON"),
        401: _problem_response("invalid-action-token"),
        403: _problem_response("HTTPS required"),
        422: _problem_response("validation-error"),
        429: _problem_response("rate-limited"),
    },
)
def activate(
    request: Request, data: ActivateAccountInput, session: SessionDependency
) -> Response:
    """Consume one activation token under IP and account rate limits."""
    if _settings(request).environment == "production" and request.url.scheme != "https":
        raise AuthHttpError(
            403, "about:blank", "Odmowa dostępu", "Niedozwolone żądanie."
        )
    plaintext = data.token.get_secret_value()
    digest = hash_action_token(plaintext)
    found = ActionTokenRepository(session).find_activation_by_hash(digest)
    key = _token_identity("account", digest, str(found.user_id) if found else None)
    limit_status = _limiter(request).check_identity(request, "activation", key)
    try:
        activate_account(session, data)
    except InvalidActionToken:
        raise AuthHttpError(
            401,
            "urn:dreamplanner:problem:invalid-action-token",
            "Token jest nieważny",
            "Nieprawidłowy token.",
        ) from None
    except AccountValidationError:
        raise AuthHttpError(
            422,
            "urn:dreamplanner:problem:validation-error",
            "Nieprawidłowe dane",
            "Nieprawidłowe dane żądania.",
        ) from None
    return Response(
        status_code=204, headers={**limit_status.headers(), "Cache-Control": "no-store"}
    )
