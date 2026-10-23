"""HTTP endpoints for account reads and account workflows."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session

from src.auth.cookies import clear_refresh_cookie
from src.auth.dependencies import (
    AuthHttpError,
    CurrentIdentity,
    require_origin,
    require_roles,
)
from src.auth.rate_limits import AuthRateLimits
from src.auth.schemas import ProblemDetails
from src.auth.session_service import ActiveIdentity, InvalidSession
from src.core.database import get_session
from src.users.account_service import (
    AccountNotFound,
    AccountStateConflict,
    AdministratorRequired,
    EmailConflict,
    create_user,
    reissue_activation_token,
)
from src.users.admin_service import (
    LastAdminConflict,
    SelfActionForbidden,
    change_role,
    deactivate_account,
)
from src.users.models import User
from src.users.password_service import InvalidCurrentPassword, change_own_password
from src.users.repository import UserRepository
from src.users.schemas import (
    ActivationTokenResponse,
    ChangePasswordInput,
    ChangeRoleInput,
    CreatedUserResponse,
    CreateUserInput,
    UserListResponse,
    UserProfile,
)

router = APIRouter(prefix="/users", tags=["users"])
SessionDependency = Annotated[Session, Depends(get_session)]
AdminIdentity = Annotated[ActiveIdentity, Depends(require_roles("admin"))]
UserIdPath = Annotated[str, Path(pattern=r"^[0-9]+$", max_length=19)]
SortField = Literal[
    "full_name", "-full_name", "email", "-email", "created_at", "-created_at"
]


def _problem_response(description: str) -> dict[str, object]:
    """Document the shared problem media type for account commands."""
    return {
        "description": description,
        "content": {
            "application/problem+json": {"schema": ProblemDetails.model_json_schema()}
        },
    }


def _admin_required() -> AuthHttpError:
    """Map a service-side role change to the normal authorization problem."""
    return AuthHttpError(403, "about:blank", "Odmowa dostępu", "Brak uprawnienia.")


def _public_user(user: User) -> UserProfile:
    """Select the approved fields from a persisted account."""
    return UserProfile(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        role=user.role.code,
        is_active=user.is_active,
        activated_at=user.activated_at,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


def _parse_user_id(user_id: str) -> int:
    """Validate a decimal path identifier within PostgreSQL BIGINT range."""
    parsed_id = int(user_id)
    if parsed_id > 9223372036854775807:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("path", "user_id")}]
        )
    return parsed_id


@router.get(
    "/me",
    response_model=UserProfile,
    summary="Pobierz własny profil",
    responses={
        401: {
            "description": "Invalid access token, session, or account",
            "content": {
                "application/problem+json": {
                    "schema": ProblemDetails.model_json_schema()
                }
            },
        }
    },
)
def read_current_user(
    response: Response, identity: CurrentIdentity, session: SessionDependency
) -> UserProfile:
    """Return current database fields for the authenticated account."""
    user = UserRepository(session).get_by_id(identity.user_id)
    if user is None or not user.is_active or user.activated_at is None:
        raise AuthHttpError(
            401,
            "about:blank",
            "Brak uwierzytelnienia",
            "Nieprawidłowe uwierzytelnienie.",
            bearer=True,
        )
    response.headers["Cache-Control"] = "no-store"
    return _public_user(user)


@router.post(
    "/me/password-change",
    status_code=204,
    summary="Zmień własne hasło",
    responses={
        400: _problem_response("Invalid JSON"),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("invalid-current-password or Origin denied"),
        422: _problem_response("validation-error"),
        429: _problem_response("rate-limited"),
    },
)
def change_current_password(
    request: Request,
    data: ChangePasswordInput,
    identity: CurrentIdentity,
    session: SessionDependency,
) -> Response:
    """Verify the existing password, replace it and end all sessions."""
    limiter: AuthRateLimits = request.app.state.auth_rate_limits
    limit_status = limiter.check_identity(
        request, "password_change", f"user:{identity.user_id}"
    )
    require_origin(request)
    try:
        change_own_password(
            session,
            identity.user_id,
            identity.session_id,
            data.current_password.get_secret_value(),
            data.new_password.get_secret_value(),
        )
    except InvalidCurrentPassword:
        raise AuthHttpError(
            403,
            "urn:dreamplanner:problem:invalid-current-password",
            "Nieprawidłowe obecne hasło",
            "Nieprawidłowe obecne hasło.",
        ) from None
    except InvalidSession:
        raise AuthHttpError(
            401,
            "about:blank",
            "Brak uwierzytelnienia",
            "Nieprawidłowe uwierzytelnienie.",
            bearer=True,
        ) from None
    response = Response(
        status_code=204,
        headers={**limit_status.headers(), "Cache-Control": "no-store"},
    )
    clear_refresh_cookie(response, request.app.state.settings)
    return response


@router.get(
    "",
    response_model=UserListResponse,
    summary="Pobierz administracyjną listę kont",
    responses={
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Administrator required"),
        422: _problem_response("validation-error"),
    },
)
def list_accounts(
    request: Request,
    response: Response,
    identity: AdminIdentity,
    session: SessionDependency,
    role: Literal["admin", "manager", "employee"] | None = None,
    is_active: bool | None = None,
    activated: bool | None = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
    sort: SortField = "full_name",
) -> UserListResponse:
    """List accounts using only the approved filters and public fields."""
    allowed = {"role", "is_active", "activated", "q", "limit", "offset", "sort"}
    unknown = set(request.query_params) - allowed
    if unknown:
        raise RequestValidationError(
            [
                {"type": "extra_forbidden", "loc": ("query", key)}
                for key in sorted(unknown)
            ]
        )
    users, total = UserRepository(session).list_users(
        role=role,
        is_active=is_active,
        activated=activated,
        query=q,
        limit=limit,
        offset=offset,
        sort=sort,
    )
    response.headers["Cache-Control"] = "no-store"
    return UserListResponse(
        items=[_public_user(user) for user in users],
        limit=limit,
        offset=offset,
        total=total,
    )


@router.get(
    "/{user_id}",
    response_model=UserProfile,
    summary="Pobierz administracyjne szczegóły konta",
    responses={
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Administrator required"),
        404: _problem_response("Account not found"),
        422: _problem_response("validation-error"),
    },
)
def read_account(
    user_id: UserIdPath,
    response: Response,
    identity: AdminIdentity,
    session: SessionDependency,
) -> UserProfile:
    """Return one existing account without exposing stored secrets."""
    user = UserRepository(session).get_by_id(_parse_user_id(user_id))
    if user is None:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono konta", "Konto nie istnieje."
        )
    response.headers["Cache-Control"] = "no-store"
    return _public_user(user)


@router.put(
    "/{user_id}/role",
    response_model=UserProfile,
    summary="Zmień rolę konta",
    responses={
        400: _problem_response("Invalid JSON"),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Administrator required or self-change forbidden"),
        404: _problem_response("Account not found"),
        409: _problem_response("last-admin-conflict"),
        422: _problem_response("validation-error"),
    },
)
def change_account_role(
    user_id: UserIdPath,
    data: ChangeRoleInput,
    response: Response,
    identity: AdminIdentity,
    session: SessionDependency,
) -> UserProfile:
    """Change another user's role after service-side administrator checks."""
    try:
        user = change_role(
            session, identity.user_id, _parse_user_id(user_id), data.role
        )
    except (AdministratorRequired, SelfActionForbidden):
        raise _admin_required() from None
    except AccountNotFound:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono konta", "Konto nie istnieje."
        ) from None
    except LastAdminConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:last-admin-conflict",
            "Konflikt administratora",
            "Operacja usunęłaby ostatniego aktywnego administratora.",
        ) from None
    response.headers["Cache-Control"] = "no-store"
    return _public_user(user)


@router.post(
    "/{user_id}/deactivation",
    response_model=UserProfile,
    summary="Dezaktywuj konto",
    responses={
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Administrator required or self-deactivation forbidden"),
        404: _problem_response("Account not found"),
        409: _problem_response("account-state-conflict or last-admin-conflict"),
        422: _problem_response("validation-error"),
    },
)
def deactivate_user(
    user_id: UserIdPath,
    response: Response,
    identity: AdminIdentity,
    session: SessionDependency,
) -> UserProfile:
    """Deactivate another user and revoke their authentication artifacts."""
    try:
        user = deactivate_account(session, identity.user_id, _parse_user_id(user_id))
    except (AdministratorRequired, SelfActionForbidden):
        raise _admin_required() from None
    except AccountNotFound:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono konta", "Konto nie istnieje."
        ) from None
    except AccountStateConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:account-state-conflict",
            "Konflikt stanu konta",
            "Konto nie jest aktywne i aktywowane.",
        ) from None
    except LastAdminConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:last-admin-conflict",
            "Konflikt administratora",
            "Operacja usunęłaby ostatniego aktywnego administratora.",
        ) from None
    response.headers["Cache-Control"] = "no-store"
    return _public_user(user)


@router.post(
    "",
    status_code=201,
    response_model=CreatedUserResponse,
    summary="Utwórz konto i wydaj token aktywacyjny",
    responses={
        400: _problem_response("Invalid JSON"),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Administrator required"),
        409: _problem_response("email-conflict"),
        422: _problem_response("validation-error"),
    },
)
def create_account(
    data: CreateUserInput,
    response: Response,
    identity: AdminIdentity,
    session: SessionDependency,
) -> CreatedUserResponse:
    """Create an inactive account and return its first token once."""
    try:
        issued = create_user(session, identity.user_id, data)
    except AdministratorRequired:
        raise _admin_required() from None
    except EmailConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:email-conflict",
            "Konflikt adresu e-mail",
            "Adres e-mail jest już używany.",
        ) from None

    user = UserRepository(session).get_by_id(issued.user_id)
    public = _public_user(user)
    response.headers["Location"] = f"/api/v1/users/{issued.user_id}"
    response.headers["Cache-Control"] = "no-store"
    return CreatedUserResponse(
        **public.model_dump(),
        activation_token=issued.activation_token.get_secret_value(),
        activation_expires_at=issued.expires_at,
    )


@router.post(
    "/{user_id}/activation-tokens",
    response_model=ActivationTokenResponse,
    summary="Wydaj ponownie token pierwszej aktywacji",
    responses={
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Administrator required"),
        404: _problem_response("Account not found"),
        409: _problem_response("account-state-conflict"),
        422: _problem_response("validation-error"),
    },
)
def reissue_activation(
    user_id: UserIdPath,
    response: Response,
    identity: AdminIdentity,
    session: SessionDependency,
) -> ActivationTokenResponse:
    """Replace a pending account's token after administrator authorization."""
    parsed_id = _parse_user_id(user_id)
    try:
        issued = reissue_activation_token(session, identity.user_id, parsed_id)
    except AdministratorRequired:
        raise _admin_required() from None
    except AccountNotFound:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono konta", "Konto nie istnieje."
        ) from None
    except AccountStateConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:account-state-conflict",
            "Konflikt stanu konta",
            "Konto nie oczekuje na pierwszą aktywację.",
        ) from None
    response.headers["Cache-Control"] = "no-store"
    return ActivationTokenResponse(
        activation_token=issued.activation_token.get_secret_value(),
        expires_at=issued.expires_at,
    )
