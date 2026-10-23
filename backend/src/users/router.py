"""HTTP endpoints for account profile and admin activation workflows."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session

from src.auth.dependencies import AuthHttpError, CurrentIdentity, require_roles
from src.auth.schemas import ProblemDetails
from src.auth.session_service import ActiveIdentity
from src.core.database import get_session
from src.users.account_service import (
    AccountNotFound,
    AccountStateConflict,
    AdministratorRequired,
    EmailConflict,
    create_user,
    reissue_activation_token,
)
from src.users.models import User
from src.users.repository import UserRepository
from src.users.schemas import (
    ActivationTokenResponse,
    CreatedUserResponse,
    CreateUserInput,
    UserProfile,
)

router = APIRouter(prefix="/users", tags=["users"])
SessionDependency = Annotated[Session, Depends(get_session)]
AdminIdentity = Annotated[ActiveIdentity, Depends(require_roles("admin"))]


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
    user_id: Annotated[str, Path(pattern=r"^[0-9]+$", max_length=19)],
    response: Response,
    identity: AdminIdentity,
    session: SessionDependency,
) -> ActivationTokenResponse:
    """Replace a pending account's token after administrator authorization."""
    parsed_id = int(user_id)
    if parsed_id > 9223372036854775807:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("path", "user_id")}]
        )
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
