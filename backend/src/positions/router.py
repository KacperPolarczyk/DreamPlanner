"""HTTP endpoints for position reads."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session

from src.auth.dependencies import AuthHttpError, require_roles
from src.auth.schemas import ProblemDetails, validation_problem_schema
from src.auth.session_service import ActiveIdentity
from src.core.database import get_session
from src.positions.models import Position
from src.positions.repository import PositionRepository
from src.positions.schemas import PositionListResponse, PositionResponse

router = APIRouter(prefix="/positions", tags=["positions"])
SessionDependency = Annotated[Session, Depends(get_session)]
ReadIdentity = Annotated[ActiveIdentity, Depends(require_roles("admin", "manager"))]
PositionIdPath = Annotated[str, Path(pattern=r"^[0-9]+$", max_length=19)]
SortField = Literal["name", "-name", "id", "-id"]
MAX_BIGINT = 9223372036854775807


def _problem_response(
    description: str, *, validation: bool = False
) -> dict[str, object]:
    """Document the shared RFC 9457 response shape."""
    schema = (
        validation_problem_schema()
        if validation
        else ProblemDetails.model_json_schema()
    )
    return {
        "description": description,
        "content": {"application/problem+json": {"schema": schema}},
    }


def _public_position(position: Position) -> PositionResponse:
    """Select only fields approved for the public position contract."""
    return PositionResponse(
        id=str(position.id), name=position.name, is_active=position.is_active
    )


@router.get(
    "",
    response_model=PositionListResponse,
    summary="Pobierz listę stanowisk",
    description=(
        "Wymaga Bearer JWT i aktywnej roli `admin` lub `manager`. "
        "Zwraca także stanowiska nieaktywne, chyba że użyto filtra `is_active`. "
        "Wyszukiwanie `q` jest niewrażliwe na wielkość liter i traktuje % oraz _ "
        "dosłownie. Sortowanie po nazwie używa kolacji bazy; remisy rozstrzyga "
        "rosnące `id`."
    ),
    responses={
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Admin or manager required"),
        422: _problem_response("validation-error", validation=True),
    },
)
def list_positions(
    request: Request,
    response: Response,
    identity: ReadIdentity,
    session: SessionDependency,
    is_active: Annotated[
        bool | None, Query(description="Filtr aktywności; bez filtra oba stany.")
    ] = None,
    q: Annotated[
        str | None,
        Query(
            description=(
                "Dosłowny fragment nazwy bez względu na wielkość liter; "
                "po obcięciu białych znaków brzegowych wymagane 1–120 znaków."
            )
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=MAX_BIGINT)] = 0,
    sort: SortField = "name",
) -> PositionListResponse:
    """Return one authorized page of active and inactive positions."""
    allowed = {"is_active", "q", "limit", "offset", "sort"}
    unknown = set(request.query_params) - allowed
    if unknown:
        raise RequestValidationError(
            [
                {"type": "extra_forbidden", "loc": ("query", key)}
                for key in sorted(unknown)
            ]
        )
    if q is not None:
        q = q.strip()
        if not 1 <= len(q) <= 120:
            raise RequestValidationError(
                [{"type": "string_length", "loc": ("query", "q")}]
            )
    positions, total = PositionRepository(session).list_positions(
        is_active=is_active, query=q, limit=limit, offset=offset, sort=sort
    )
    response.headers["Cache-Control"] = "no-store"
    return PositionListResponse(
        items=[_public_position(position) for position in positions],
        limit=limit,
        offset=offset,
        total=total,
    )


@router.get(
    "/{position_id}",
    response_model=PositionResponse,
    summary="Pobierz szczegóły stanowiska",
    description=(
        "Wymaga Bearer JWT i aktywnej roli `admin` lub `manager`. "
        "Zwraca także stanowisko nieaktywne."
    ),
    responses={
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Admin or manager required"),
        404: _problem_response("Position not found"),
        422: _problem_response("validation-error", validation=True),
    },
)
def read_position(
    position_id: PositionIdPath,
    response: Response,
    identity: ReadIdentity,
    session: SessionDependency,
) -> PositionResponse:
    """Return one existing position, including an inactive one."""
    parsed_id = int(position_id)
    if parsed_id > MAX_BIGINT:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("path", "position_id")}]
        )
    position = PositionRepository(session).get_by_id(parsed_id)
    if position is None:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono stanowiska", "Stanowisko nie istnieje."
        )
    response.headers["Cache-Control"] = "no-store"
    return _public_position(position)
