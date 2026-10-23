"""HTTP endpoints for authenticated account profile reads."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from src.auth.dependencies import AuthHttpError, CurrentIdentity
from src.auth.schemas import ProblemDetails
from src.core.database import get_session
from src.users.repository import UserRepository
from src.users.schemas import UserProfile

router = APIRouter(prefix="/users", tags=["users"])
SessionDependency = Annotated[Session, Depends(get_session)]


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
