"""Read-only directory of employees eligible for manager assignments."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session

from src.auth.dependencies import require_roles
from src.auth.schemas import ProblemDetails, validation_problem_schema
from src.auth.session_service import ActiveIdentity
from src.core.database import get_session
from src.users.repository import UserRepository
from src.users.schemas import EmployeeListItem, EmployeeListResponse

router = APIRouter(prefix="/employees", tags=["employees"])
SessionDependency = Annotated[Session, Depends(get_session)]
ReadIdentity = Annotated[ActiveIdentity, Depends(require_roles("manager", "admin"))]
NO_STORE_HEADER = {
    "Cache-Control": {
        "description": "Protected responses are not cacheable.",
        "schema": {"type": "string", "const": "no-store"},
    }
}


def _problem_response(description: str, *, validation: bool = False) -> dict:
    """Document the shared RFC 9457 error response and cache header."""
    schema = (
        validation_problem_schema()
        if validation
        else ProblemDetails.model_json_schema()
    )
    return {
        "description": description,
        "content": {"application/problem+json": {"schema": schema}},
        "headers": NO_STORE_HEADER,
    }


def _unauthenticated_response() -> dict:
    """Document the Bearer challenge returned by the auth dependency."""
    response = _problem_response("Invalid access token or session")
    response["headers"] = {
        **NO_STORE_HEADER,
        "WWW-Authenticate": {
            "description": "Bearer authentication challenge.",
            "schema": {"type": "string", "const": "Bearer"},
        },
    }
    return response


@router.get(
    "",
    response_model=EmployeeListResponse,
    summary="Pobierz pracowników do przydziałów",
    description=(
        "Wymaga Bearer JWT i aktualnej roli manager lub admin. Zwraca wszystkich "
        "aktywnych, aktywowanych użytkowników z rolą employee. q wyszukuje "
        "dosłowny podciąg wyłącznie w full_name, bez rozróżniania wielkości "
        "liter; puste q nie zawęża listy. Kolejność: full_name ASC, user_id "
        "ASC. total liczy wyniki po filtrach przed paginacją."
    ),
    responses={
        200: {"description": "Successful Response", "headers": NO_STORE_HEADER},
        401: _unauthenticated_response(),
        403: _problem_response("Manager or admin required"),
        422: _problem_response("validation-error", validation=True),
    },
)
def list_employees(
    request: Request,
    response: Response,
    identity: ReadIdentity,
    session: SessionDependency,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
) -> EmployeeListResponse:
    """Return a bounded page of eligible employees with no account details."""
    allowed = {"q", "limit", "offset"}
    values = request.query_params
    invalid = (set(values) - allowed) | {
        key for key in set(values) & allowed if len(values.getlist(key)) > 1
    }
    if invalid:
        raise RequestValidationError(
            [
                {"type": "extra_forbidden", "loc": ("query", key)}
                for key in sorted(invalid)
            ]
        )

    rows, total = UserRepository(session).list_eligible_employees(
        query=q, limit=limit, offset=offset
    )
    response.headers["Cache-Control"] = "no-store"
    return EmployeeListResponse(
        items=[
            EmployeeListItem(user_id=str(user_id), full_name=full_name)
            for user_id, full_name in rows
        ],
        limit=limit,
        offset=offset,
        total=total,
    )
