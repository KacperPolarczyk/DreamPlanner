"""HTTP endpoints for monthly schedule reads."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session

from src.auth.dependencies import AuthHttpError, require_roles
from src.auth.schemas import ProblemDetails, validation_problem_schema
from src.auth.session_service import ActiveIdentity
from src.core.database import get_session
from src.schedules.models import Schedule
from src.schedules.repository import ScheduleRepository
from src.schedules.schemas import (
    CreateScheduleInput,
    ScheduleListResponse,
    ScheduleResponse,
    ScheduleStatus,
)
from src.schedules.service import (
    ScheduleNotEmpty,
    ScheduleNotFound,
    SchedulePeriodConflict,
    ScheduleStateConflict,
    create_schedule,
    delete_empty_draft,
)

router = APIRouter(prefix="/schedules", tags=["schedules"])
SessionDependency = Annotated[Session, Depends(get_session)]
ReadIdentity = Annotated[ActiveIdentity, Depends(require_roles("admin", "manager"))]
ManagerIdentity = Annotated[ActiveIdentity, Depends(require_roles("manager"))]
ScheduleIdPath = Annotated[str, Path(pattern=r"^[0-9]+$", max_length=19)]
SortField = Literal["year", "-year", "month", "-month"]
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


def _utc(value: datetime | None) -> datetime | None:
    """Serialize PostgreSQL event timestamps in UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _public_schedule(schedule: Schedule) -> ScheduleResponse:
    """Select only fields approved for the public schedule contract."""
    return ScheduleResponse(
        id=str(schedule.id),
        year=schedule.year,
        month=schedule.month,
        status=schedule.status,
        created_by=str(schedule.created_by),
        created_at=_utc(schedule.created_at),
        collection_opened_at=_utc(schedule.collection_opened_at),
        collection_closed_at=_utc(schedule.collection_closed_at),
        published_at=_utc(schedule.published_at),
    )


def _parse_schedule_id(schedule_id: str) -> int:
    """Validate a decimal path identifier within PostgreSQL BIGINT range."""
    parsed_id = int(schedule_id)
    if parsed_id > MAX_BIGINT:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("path", "schedule_id")}]
        )
    return parsed_id


async def _reject_nonempty_body(request: Request) -> None:
    """Reject content on a bodyless schedule operation."""
    if await request.body():
        raise RequestValidationError([{"type": "extra_forbidden", "loc": ("body",)}])


@router.post(
    "",
    status_code=201,
    response_model=ScheduleResponse,
    summary="Utwórz miesięczny grafik",
    description=(
        "Wymaga Bearer JWT i aktywnej roli `manager`. Tworzy pusty grafik "
        "w stanie `draft` dla dowolnego miesiąca z lat 2000–2100. "
        "Okres jest globalnie unikalny. Sukces zwraca `Location` i "
        "`Cache-Control: no-store`."
    ),
    responses={
        400: _problem_response("Invalid JSON"),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Manager required"),
        409: _problem_response("schedule-period-conflict"),
        422: _problem_response("validation-error", validation=True),
    },
)
def create_new_schedule(
    data: CreateScheduleInput,
    response: Response,
    identity: ManagerIdentity,
    session: SessionDependency,
) -> ScheduleResponse:
    """Create a monthly draft under the current manager's identity."""
    try:
        schedule = create_schedule(session, data.year, data.month, identity.user_id)
    except SchedulePeriodConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:schedule-period-conflict",
            "Okres grafiku jest już zajęty",
            "Grafik dla tego roku i miesiąca już istnieje.",
        ) from None
    response.headers["Location"] = f"/api/v1/schedules/{schedule.id}"
    response.headers["Cache-Control"] = "no-store"
    return _public_schedule(schedule)


@router.get(
    "",
    response_model=ScheduleListResponse,
    summary="Pobierz listę grafików",
    description=(
        "Wymaga Bearer JWT i aktywnej roli `admin` lub `manager`. "
        "Obejmuje wszystkie grafiki, także nieopublikowane i utworzone przez "
        "innego managera. Bez `sort`: najnowszy okres pierwszy."
    ),
    responses={
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Admin or manager required"),
        422: _problem_response("validation-error", validation=True),
    },
)
def list_schedules(
    request: Request,
    response: Response,
    identity: ReadIdentity,
    session: SessionDependency,
    year: Annotated[int | None, Query(ge=2000, le=2100)] = None,
    month: Annotated[int | None, Query(ge=1, le=12)] = None,
    status: ScheduleStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=MAX_BIGINT)] = 0,
    sort: SortField | None = None,
) -> ScheduleListResponse:
    """Return one authorized page of schedules and its filtered total."""
    allowed = {"year", "month", "status", "limit", "offset", "sort"}
    unknown = set(request.query_params) - allowed
    repeated = {key for key in allowed if len(request.query_params.getlist(key)) > 1}
    if unknown or repeated:
        raise RequestValidationError(
            [
                {"type": "extra_forbidden", "loc": ("query", key)}
                for key in sorted(unknown | repeated)
            ]
        )
    non_decimal = {
        key
        for key in ("year", "month", "limit", "offset")
        if (value := request.query_params.get(key)) is not None
        and (not value.isascii() or not value.isdecimal())
    }
    if non_decimal:
        raise RequestValidationError(
            [
                {"type": "int_parsing", "loc": ("query", key)}
                for key in sorted(non_decimal)
            ]
        )
    schedules, total = ScheduleRepository(session).list_schedules(
        year=year,
        month=month,
        status=status,
        limit=limit,
        offset=offset,
        sort=sort,
    )
    response.headers["Cache-Control"] = "no-store"
    return ScheduleListResponse(
        items=[_public_schedule(schedule) for schedule in schedules],
        limit=limit,
        offset=offset,
        total=total,
    )


@router.get(
    "/{schedule_id}",
    response_model=ScheduleResponse,
    summary="Pobierz szczegóły grafiku",
    description=(
        "Wymaga Bearer JWT i aktywnej roli `admin` lub `manager`. "
        "Zwraca grafik w dowolnym stanie bez danych potomnych."
    ),
    responses={
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Admin or manager required"),
        404: _problem_response("Schedule not found"),
        422: _problem_response("validation-error", validation=True),
    },
)
def read_schedule(
    schedule_id: ScheduleIdPath,
    response: Response,
    identity: ReadIdentity,
    session: SessionDependency,
) -> ScheduleResponse:
    """Return one existing schedule regardless of its status or creator."""
    schedule = ScheduleRepository(session).get_by_id(_parse_schedule_id(schedule_id))
    if schedule is None:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono grafiku", "Grafik nie istnieje."
        )
    response.headers["Cache-Control"] = "no-store"
    return _public_schedule(schedule)


@router.delete(
    "/{schedule_id}",
    status_code=204,
    summary="Usuń pusty szkic grafiku",
    description=(
        "Wymaga Bearer JWT i aktywnej roli `manager`. Usuwa wyłącznie "
        "pusty grafik `draft`, po zablokowaniu wiersza i sprawdzeniu "
        "zapotrzebowania oraz zgłoszeń dyspozycyjności. Sukces nie ma "
        "treści i zawiera `Cache-Control: no-store`."
    ),
    responses={
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Manager required"),
        404: _problem_response("Schedule not found"),
        409: _problem_response("schedule-state-conflict or schedule-not-empty"),
        422: _problem_response("validation-error", validation=True),
    },
)
def delete_schedule(
    schedule_id: ScheduleIdPath,
    response: Response,
    identity: ManagerIdentity,
    body_is_empty: Annotated[None, Depends(_reject_nonempty_body)],
    session: SessionDependency,
) -> None:
    """Delete one empty draft after its transaction commits."""
    try:
        delete_empty_draft(session, _parse_schedule_id(schedule_id))
    except ScheduleNotFound:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono grafiku", "Grafik nie istnieje."
        ) from None
    except ScheduleStateConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:schedule-state-conflict",
            "Nie można usunąć grafiku",
            "Usunąć można tylko grafik w stanie draft.",
        ) from None
    except ScheduleNotEmpty:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:schedule-not-empty",
            "Grafik nie jest pusty",
            "Grafik zawiera dane powiązane i nie może zostać usunięty.",
        ) from None
    response.headers["Cache-Control"] = "no-store"
