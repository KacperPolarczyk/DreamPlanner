"""HTTP endpoints for monthly schedule reads."""

from datetime import UTC, date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session

from src.auth.dependencies import (
    AuthHttpError,
    require_manager_write,
    require_roles,
    require_schedule_delete_write,
)
from src.auth.schemas import ProblemDetails, validation_problem_schema
from src.auth.session_service import ActiveIdentity
from src.core.database import get_session
from src.positions.models import Position
from src.schedules.models import Schedule, ShiftRequirement
from src.schedules.repository import ScheduleRepository, ShiftRequirementRepository
from src.schedules.schemas import (
    CreateScheduleInput,
    CreateShiftRequirementInput,
    EmployeeScheduleItem,
    EmployeeScheduleListResponse,
    ScheduleListResponse,
    ScheduleResponse,
    ScheduleStatus,
    ShiftRequirementListResponse,
    ShiftRequirementResponse,
    UpdateShiftRequirementInput,
)
from src.schedules.service import (
    PositionInactive,
    PositionNotFound,
    ScheduleNotFound,
    SchedulePeriodConflict,
    SchedulePublicationBlocked,
    SchedulePublicationForbidden,
    ScheduleStateConflict,
    ShiftRequirementAssigned,
    ShiftRequirementConflict,
    ShiftRequirementNotFound,
    close_collection,
    close_schedule,
    create_schedule,
    create_shift_requirement,
    delete_shift_requirement,
    open_collection,
    publish_schedule,
    update_shift_requirement,
)
from src.schedules.service import (
    delete_schedule as delete_schedule_record,
)

router = APIRouter(prefix="/schedules", tags=["schedules"])
employee_router = APIRouter(prefix="/me", tags=["my-schedules"])
SessionDependency = Annotated[Session, Depends(get_session)]
ReadIdentity = Annotated[ActiveIdentity, Depends(require_roles("admin", "manager"))]
ManagerIdentity = Annotated[ActiveIdentity, Depends(require_manager_write)]
DeleteIdentity = Annotated[ActiveIdentity, Depends(require_schedule_delete_write)]
EmployeeIdentity = Annotated[ActiveIdentity, Depends(require_roles("employee"))]
ScheduleIdPath = Annotated[str, Path(pattern=r"^[0-9]+$", max_length=19)]
SortField = Literal["year", "-year", "month", "-month"]
MAX_BIGINT = 9223372036854775807
_NO_STORE_HEADER = {
    "Cache-Control": {
        "description": "Protected responses are not cacheable.",
        "schema": {"type": "string", "const": "no-store"},
    }
}


def _success_response(status: int) -> dict[str, object]:
    """Document headers returned by successful schedule operations."""
    headers = dict(_NO_STORE_HEADER)
    if status == 201:
        headers["Location"] = {
            "description": "Canonical path of the created resource.",
            "schema": {"type": "string"},
        }
    return {
        "description": "No Content" if status == 204 else "Successful Response",
        "headers": headers,
    }


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
        "headers": _NO_STORE_HEADER,
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


def _public_shift_requirement(
    requirement: ShiftRequirement, position: Position
) -> ShiftRequirementResponse:
    """Select only fields approved for the public requirement contract."""
    return ShiftRequirementResponse(
        id=str(requirement.id),
        schedule_id=str(requirement.schedule_id),
        position_id=str(requirement.position_id),
        position_name=position.name,
        position_is_active=position.is_active,
        work_date=requirement.work_date,
        start_time=requirement.start_time,
        end_time=requirement.end_time,
        required_workers=requirement.required_workers,
        note=requirement.note,
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


def _parse_requirement_id(requirement_id: str) -> int:
    """Validate a decimal path identifier within PostgreSQL BIGINT range."""
    parsed_id = int(requirement_id)
    if parsed_id > MAX_BIGINT:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("path", "requirement_id")}]
        )
    return parsed_id


@router.get(
    "/{schedule_id}/shift-requirements",
    response_model=ShiftRequirementListResponse,
    summary="Pobierz zapotrzebowanie grafiku",
    description=(
        "Wymaga Bearer JWT i aktywnej roli `admin` lub `manager`. Zwraca "
        "zapotrzebowanie niezależnie od statusu i twórcy grafiku. Filtry "
        "`work_date` i `position_id` można łączyć. Kolejność jest stała: "
        "`work_date ASC, start_time ASC, id ASC`; `total` liczy wyniki "
        "po filtrach przed paginacją. `work_date` wymaga `YYYY-MM-DD`, "
        "`position_id` dodatniego BIGINT, a identyfikator grafiku cyfr ASCII "
        "w zakresie 0-9223372036854775807 (do 19 cyfr). Godziny odpowiedzi "
        "to `HH:MM:SS` w `Europe/Warsaw`. Odpowiedź zawiera "
        "`Cache-Control: no-store`."
    ),
    responses={
        200: _success_response(200),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Admin or manager required"),
        404: _problem_response("Schedule not found"),
        422: _problem_response("validation-error", validation=True),
    },
)
def list_shift_requirements(
    schedule_id: ScheduleIdPath,
    request: Request,
    response: Response,
    identity: ReadIdentity,
    session: SessionDependency,
    work_date: Annotated[
        str | None, Query(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    ] = None,
    position_id: Annotated[
        str | None, Query(pattern=r"^[0-9]+$", max_length=19)
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=MAX_BIGINT)] = 0,
) -> ShiftRequirementListResponse:
    """Return one authorized, filtered page of requirements for a schedule."""
    allowed = {"work_date", "position_id", "limit", "offset"}
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
        for key in ("limit", "offset")
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

    try:
        parsed_work_date = date.fromisoformat(work_date) if work_date else None
    except ValueError:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", "work_date")}]
        ) from None

    parsed_position_id = int(position_id) if position_id is not None else None
    if parsed_position_id == 0 or (
        parsed_position_id is not None and parsed_position_id > MAX_BIGINT
    ):
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", "position_id")}]
        )

    parsed_schedule_id = _parse_schedule_id(schedule_id)
    if ScheduleRepository(session).get_by_id(parsed_schedule_id) is None:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono grafiku", "Grafik nie istnieje."
        )

    rows, total = ShiftRequirementRepository(session).list_for_schedule(
        parsed_schedule_id,
        work_date=parsed_work_date,
        position_id=parsed_position_id,
        limit=limit,
        offset=offset,
    )
    response.headers["Cache-Control"] = "no-store"
    return ShiftRequirementListResponse(
        items=[
            _public_shift_requirement(requirement, position)
            for requirement, position in rows
        ],
        limit=limit,
        offset=offset,
        total=total,
    )


@router.get(
    "/{schedule_id}/shift-requirements/{requirement_id}",
    response_model=ShiftRequirementResponse,
    summary="Pobierz szczegóły zapotrzebowania",
    description=(
        "Wymaga Bearer JWT i aktywnej roli `admin` lub `manager`. Zwraca "
        "pozycję tylko wtedy, gdy należy do wskazanego grafiku. Odczyt jest "
        "dozwolony w każdym statusie grafiku oraz dla nieaktywnego stanowiska. "
        "Identyfikatory ścieżki to cyfry ASCII w zakresie "
        "0-9223372036854775807 (do 19 cyfr). Godziny odpowiedzi to "
        "`HH:MM:SS` w `Europe/Warsaw`. "
        "Odpowiedź zawiera `Cache-Control: no-store`."
    ),
    responses={
        200: _success_response(200),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Admin or manager required"),
        404: _problem_response("Schedule or requirement not found"),
        422: _problem_response("validation-error", validation=True),
    },
)
def read_shift_requirement(
    schedule_id: ScheduleIdPath,
    requirement_id: Annotated[str, Path(pattern=r"^[0-9]+$", max_length=19)],
    response: Response,
    identity: ReadIdentity,
    session: SessionDependency,
) -> ShiftRequirementResponse:
    """Return one requirement scoped to its schedule, including position data."""
    parsed_schedule_id = _parse_schedule_id(schedule_id)
    parsed_requirement_id = _parse_requirement_id(requirement_id)
    schedule = ScheduleRepository(session).get_by_id(parsed_schedule_id)
    if schedule is None:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono zapotrzebowania", "Zasób nie istnieje."
        )
    row = ShiftRequirementRepository(session).get_for_schedule(
        parsed_schedule_id, parsed_requirement_id
    )
    if row is None:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono zapotrzebowania", "Zasób nie istnieje."
        )
    response.headers["Cache-Control"] = "no-store"
    return _public_shift_requirement(*row)


@router.post(
    "/{schedule_id}/shift-requirements",
    status_code=201,
    response_model=ShiftRequirementResponse,
    summary="Utwórz zapotrzebowanie na pracowników",
    description=(
        "Wymaga roli manager i grafiku draft. Daty i lokalne godziny "
        "wymagają `YYYY-MM-DD` i `HH:MM:SS` w `Europe/Warsaw`; przedziały "
        "mogą się nakładać. Identyfikator grafiku to cyfry ASCII w zakresie "
        "0-9223372036854775807 (do 19 cyfr), a `position_id` to dodatni "
        "dziesiętny string BIGINT. "
        "Odpowiedź zwraca Location oraz Cache-Control: no-store."
    ),
    responses={
        201: _success_response(201),
        400: _problem_response("Invalid JSON"),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Manager required"),
        404: _problem_response("Schedule or position not found"),
        409: _problem_response(
            "schedule-state-conflict, position-inactive or duplicate"
        ),
        422: _problem_response("validation-error", validation=True),
    },
)
def create_new_shift_requirement(
    schedule_id: ScheduleIdPath,
    data: CreateShiftRequirementInput,
    response: Response,
    identity: ManagerIdentity,
    session: SessionDependency,
) -> ShiftRequirementResponse:
    """Create one staffing requirement under a locked draft schedule."""
    try:
        requirement, position = create_shift_requirement(
            session,
            _parse_schedule_id(schedule_id),
            position_id=int(data.position_id),
            work_date=data.work_date,
            start_time=data.start_time,
            end_time=data.end_time,
            required_workers=data.required_workers,
            note=data.note,
        )
    except ScheduleNotFound:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono grafiku", "Grafik nie istnieje."
        ) from None
    except ScheduleStateConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:schedule-state-conflict",
            "Grafik nie pozwala na tę operację",
            "Zapotrzebowanie można zmieniać tylko w grafiku draft.",
        ) from None
    except PositionNotFound:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono stanowiska", "Stanowisko nie istnieje."
        ) from None
    except PositionInactive:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:position-inactive",
            "Stanowisko jest nieaktywne",
            "Nowe zapotrzebowanie wymaga aktywnego stanowiska.",
        ) from None
    except ShiftRequirementConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:shift-requirement-conflict",
            "Zapotrzebowanie już istnieje",
            "Zapotrzebowanie o tych samych danych już istnieje.",
        ) from None
    except ValueError as exc:
        raise AuthHttpError(
            422,
            "urn:dreamplanner:problem:validation-error",
            "Błędne dane wejściowe",
            str(exc),
        ) from None
    response.headers["Location"] = (
        f"/api/v1/schedules/{requirement.schedule_id}/shift-requirements/{requirement.id}"
    )
    response.headers["Cache-Control"] = "no-store"
    return _public_shift_requirement(requirement, position)


@router.patch(
    "/{schedule_id}/shift-requirements/{requirement_id}",
    response_model=ShiftRequirementResponse,
    summary="Edytuj zapotrzebowanie na pracowników",
    description=(
        "Wymaga roli manager oraz grafiku draft. Zmienia tylko przesłane pola. "
        "Jeśli istnieją przydziały, dozwolona jest zmiana wyłącznie notatki lub "
        "wartości strukturalnych bez rzeczywistej zmiany. Odpowiedź zawiera "
        "Cache-Control: no-store. Identyfikatory ścieżki to cyfry ASCII w "
        "zakresie 0-9223372036854775807 (do 19 cyfr); `position_id` jest "
        "dodatnim stringiem BIGINT. Daty i godziny wymagają `YYYY-MM-DD` oraz "
        "`HH:MM:SS` w `Europe/Warsaw`."
    ),
    responses={
        200: _success_response(200),
        400: _problem_response("Invalid JSON"),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Manager required"),
        404: _problem_response("Schedule, requirement or position not found"),
        409: _problem_response(
            "schedule-state-conflict, position-inactive, assigned or duplicate"
        ),
        422: _problem_response("validation-error", validation=True),
    },
)
def update_existing_shift_requirement(
    schedule_id: ScheduleIdPath,
    requirement_id: Annotated[str, Path(pattern=r"^[0-9]+$", max_length=19)],
    data: UpdateShiftRequirementInput,
    response: Response,
    identity: ManagerIdentity,
    session: SessionDependency,
) -> ShiftRequirementResponse:
    """Update a requirement after locking its parent and row."""
    try:
        requirement, position = update_shift_requirement(
            session,
            _parse_schedule_id(schedule_id),
            _parse_requirement_id(requirement_id),
            changes=data.model_dump(exclude_unset=True),
        )
    except (ScheduleNotFound, ShiftRequirementNotFound):
        raise AuthHttpError(
            404,
            "about:blank",
            "Nie znaleziono zapotrzebowania",
            "Zasób nie istnieje.",
        ) from None
    except ScheduleStateConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:schedule-state-conflict",
            "Grafik nie pozwala na tę operację",
            "Zapotrzebowanie można zmieniać tylko w grafiku draft.",
        ) from None
    except PositionNotFound:
        raise AuthHttpError(
            404,
            "about:blank",
            "Nie znaleziono stanowiska",
            "Stanowisko nie istnieje.",
        ) from None
    except PositionInactive:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:position-inactive",
            "Stanowisko jest nieaktywne",
            "Nowe zapotrzebowanie wymaga aktywnego stanowiska.",
        ) from None
    except ShiftRequirementAssigned:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:shift-requirement-assigned",
            "Zapotrzebowanie ma przydziały",
            "Struktury zapotrzebowania z przydziałami nie można zmienić.",
        ) from None
    except ShiftRequirementConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:shift-requirement-conflict",
            "Zapotrzebowanie już istnieje",
            "Zapotrzebowanie o tych samych danych już istnieje.",
        ) from None
    except ValueError as exc:
        raise AuthHttpError(
            422,
            "urn:dreamplanner:problem:validation-error",
            "Błędne dane wejściowe",
            str(exc),
        ) from None
    response.headers["Cache-Control"] = "no-store"
    return _public_shift_requirement(requirement, position)


@router.delete(
    "/{schedule_id}/shift-requirements/{requirement_id}",
    status_code=204,
    summary="Usuń nieprzydzielone zapotrzebowanie",
    description=(
        "Wymaga aktywnej roli `manager` oraz grafiku `draft`. Usuwa wyłącznie "
        "zapotrzebowanie bez powiązanych przydziałów; odpowiedź 204 nie ma "
        "treści i zawiera `Cache-Control: no-store`. Identyfikatory ścieżki "
        "to cyfry ASCII w zakresie 0-9223372036854775807 (do 19 cyfr)."
    ),
    responses={
        204: _success_response(204),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Manager required"),
        404: _problem_response("Schedule or requirement not found"),
        409: _problem_response("schedule-state-conflict or shift-requirement-assigned"),
        422: _problem_response("validation-error", validation=True),
    },
)
def delete_existing_shift_requirement(
    schedule_id: ScheduleIdPath,
    requirement_id: Annotated[str, Path(pattern=r"^[0-9]+$", max_length=19)],
    response: Response,
    identity: ManagerIdentity,
    body_is_empty: Annotated[None, Depends(_reject_nonempty_body)],
    session: SessionDependency,
) -> None:
    """Delete one unassigned requirement after its transaction commits."""
    try:
        delete_shift_requirement(
            session,
            _parse_schedule_id(schedule_id),
            _parse_requirement_id(requirement_id),
        )
    except (ScheduleNotFound, ShiftRequirementNotFound):
        raise AuthHttpError(
            404,
            "about:blank",
            "Nie znaleziono zapotrzebowania",
            "Zasób nie istnieje.",
        ) from None
    except ScheduleStateConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:schedule-state-conflict",
            "Grafik nie pozwala na tę operację",
            "Zapotrzebowanie można usuwać tylko w grafiku draft.",
        ) from None
    except ShiftRequirementAssigned:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:shift-requirement-assigned",
            "Zapotrzebowanie ma przydziały",
            "Zapotrzebowania z istniejącymi przydziałami nie można usunąć.",
        ) from None
    response.headers["Cache-Control"] = "no-store"


@router.post(
    "",
    status_code=201,
    response_model=ScheduleResponse,
    summary="Utwórz miesięczny grafik",
    description=(
        "Wymaga Bearer JWT i aktywnej roli `manager`. Tworzy pusty grafik "
        "w stanie `draft` dla dowolnego miesiąca z lat 2000-2100. "
        "Okres jest globalnie unikalny. Sukces zwraca `Location` i "
        "`Cache-Control: no-store`."
    ),
    responses={
        201: _success_response(201),
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
        200: _success_response(200),
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


@employee_router.get(
    "/schedules",
    response_model=EmployeeScheduleListResponse,
    summary="Pobierz grafiki dostępne do zgłoszenia dyspozycyjności",
    description=(
        "Wymaga Bearer JWT i aktywnej roli `employee`. Zwraca wyłącznie "
        "grafiki `collecting`, bez danych innych pracowników. Kolejność: "
        "rok i miesiąc malejąco, ID malejąco. `total` jest liczone przed paginacją."
    ),
    responses={
        200: _success_response(200),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Employee required"),
        422: _problem_response("validation-error", validation=True),
    },
)
def list_employee_schedules(
    request: Request,
    response: Response,
    identity: EmployeeIdentity,
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=MAX_BIGINT)] = 0,
) -> EmployeeScheduleListResponse:
    """Return a bounded page of collecting schedules with minimal fields."""
    allowed = {"limit", "offset"}
    invalid = (set(request.query_params) - allowed) | {
        key for key in allowed if len(request.query_params.getlist(key)) > 1
    }
    invalid_decimal = {
        key
        for key in allowed
        if (value := request.query_params.get(key)) is not None
        and (not value.isascii() or not value.isdecimal())
    }
    if invalid or invalid_decimal:
        raise RequestValidationError(
            [
                {"type": "extra_forbidden", "loc": ("query", key)}
                for key in sorted(invalid)
            ]
            + [
                {"type": "int_parsing", "loc": ("query", key)}
                for key in sorted(invalid_decimal)
            ]
        )
    schedules, total = ScheduleRepository(session).list_schedules(
        year=None,
        month=None,
        status="collecting",
        limit=limit,
        offset=offset,
        sort=None,
    )
    response.headers["Cache-Control"] = "no-store"
    return EmployeeScheduleListResponse(
        items=[
            EmployeeScheduleItem(
                id=str(schedule.id),
                year=schedule.year,
                month=schedule.month,
                status="collecting",
            )
            for schedule in schedules
        ],
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
        200: _success_response(200),
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
    summary="Usuń trwale grafik",
    description=(
        "Wymaga Bearer JWT i aktywnej roli `admin` lub `manager`. "
        "Trwale usuwa grafik w dowolnym stanie wraz z zapotrzebowaniami, "
        "dyspozycyjnościami i przydziałami. Sukces nie ma treści "
        "i zawiera `Cache-Control: no-store`."
    ),
    responses={
        204: _success_response(204),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Admin or manager required"),
        404: _problem_response("Schedule not found"),
        422: _problem_response("validation-error", validation=True),
    },
)
def delete_schedule(
    schedule_id: ScheduleIdPath,
    response: Response,
    identity: DeleteIdentity,
    body_is_empty: Annotated[None, Depends(_reject_nonempty_body)],
    session: SessionDependency,
) -> None:
    """Hard delete one schedule and its dependent rows."""
    try:
        delete_schedule_record(session, _parse_schedule_id(schedule_id))
    except ScheduleNotFound:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono grafiku", "Grafik nie istnieje."
        ) from None
    response.headers["Cache-Control"] = "no-store"


@router.post(
    "/{schedule_id}/collection-opening",
    response_model=ScheduleResponse,
    summary="Otwórz zbieranie dyspozycyjności",
    description=(
        "Wymaga Bearer JWT i aktywnej roli `manager`. Bez treści żądania. "
        "Przenosi wyłącznie grafik `draft` do `collecting` i ustawia "
        "`collection_opened_at` na aktualny czas UTC. Nie wymaga "
        "zapotrzebowania. Odpowiedzi zawierają `Cache-Control: no-store`."
    ),
    responses={
        200: _success_response(200),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Manager required"),
        404: _problem_response("Schedule not found"),
        409: _problem_response("schedule-state-conflict"),
        422: _problem_response("validation-error", validation=True),
    },
)
def open_schedule_collection(
    schedule_id: ScheduleIdPath,
    response: Response,
    identity: ManagerIdentity,
    body_is_empty: Annotated[None, Depends(_reject_nonempty_body)],
    session: SessionDependency,
) -> ScheduleResponse:
    """Open collection for a draft after its transaction commits."""
    try:
        schedule = open_collection(session, _parse_schedule_id(schedule_id))
    except ScheduleNotFound:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono grafiku", "Grafik nie istnieje."
        ) from None
    except ScheduleStateConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:schedule-state-conflict",
            "Niedozwolona zmiana stanu grafiku",
            "Zbieranie dyspozycyjności można otworzyć tylko dla grafiku draft.",
        ) from None
    response.headers["Cache-Control"] = "no-store"
    return _public_schedule(schedule)


@router.post(
    "/{schedule_id}/collection-closing",
    response_model=ScheduleResponse,
    summary="Zamknij zbieranie dyspozycyjności",
    description=(
        "Wymaga Bearer JWT i aktywnej roli `manager`. Bez treści żądania. "
        "Przenosi wyłącznie grafik `collecting` do `collection_closed` i ustawia "
        "`collection_closed_at` na aktualny czas UTC. Nie wymaga zgłoszeń "
        "dyspozycyjności ani zapotrzebowania. Odpowiedzi zawierają "
        "`Cache-Control: no-store`."
    ),
    responses={
        200: _success_response(200),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Manager required"),
        404: _problem_response("Schedule not found"),
        409: _problem_response("schedule-state-conflict"),
        422: _problem_response("validation-error", validation=True),
    },
)
def close_schedule_collection(
    schedule_id: ScheduleIdPath,
    response: Response,
    identity: ManagerIdentity,
    body_is_empty: Annotated[None, Depends(_reject_nonempty_body)],
    session: SessionDependency,
) -> ScheduleResponse:
    """Close collection for a collecting schedule after commit."""
    try:
        schedule = close_collection(session, _parse_schedule_id(schedule_id))
    except ScheduleNotFound:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono grafiku", "Grafik nie istnieje."
        ) from None
    except ScheduleStateConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:schedule-state-conflict",
            "Niedozwolona zmiana stanu grafiku",
            "Zbieranie dyspozycyjności można zamknąć tylko dla grafiku collecting.",
        ) from None
    response.headers["Cache-Control"] = "no-store"
    return _public_schedule(schedule)


@router.post(
    "/{schedule_id}/closing",
    response_model=ScheduleResponse,
    summary="Przejdź do obsadzania zmian",
    description=(
        "Wymaga Bearer JWT i aktywnej roli `manager`. Bez treści żądania. "
        "Przenosi wyłącznie grafik `collection_closed` do `closed`, w którym "
        "manager może rozpocząć obsadzanie zmian. Nie ustawia nowego "
        "znacznika czasu ani nie wymaga zapotrzebowania, dyspozycyjności "
        "lub przydziałów. Odpowiedzi zawierają `Cache-Control: no-store`."
    ),
    responses={
        200: _success_response(200),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Manager required"),
        404: _problem_response("Schedule not found"),
        409: _problem_response("schedule-state-conflict"),
        422: _problem_response("validation-error", validation=True),
    },
)
def close_schedule_stage(
    schedule_id: ScheduleIdPath,
    response: Response,
    identity: ManagerIdentity,
    body_is_empty: Annotated[None, Depends(_reject_nonempty_body)],
    session: SessionDependency,
) -> ScheduleResponse:
    """Close one collection stage after its transaction commits."""
    try:
        schedule = close_schedule(session, _parse_schedule_id(schedule_id))
    except ScheduleNotFound:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono grafiku", "Grafik nie istnieje."
        ) from None
    except ScheduleStateConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:schedule-state-conflict",
            "Niedozwolona zmiana stanu grafiku",
            "Grafik można zamknąć tylko po zakończeniu zbierania dyspozycyjności.",
        ) from None
    response.headers["Cache-Control"] = "no-store"
    return _public_schedule(schedule)


@router.post(
    "/{schedule_id}/publication",
    response_model=ScheduleResponse,
    summary="Opublikuj grafik",
    description=(
        "Wymaga Bearer JWT oraz aktualnej roli aktywnego managera. Operacja "
        "nie przyjmuje body i publikuje wyłącznie grafik `closed`. Grafik musi "
        "zawierać co najmniej jedno zapotrzebowanie. Częściowa obsada i brak "
        "przydziałów dla wymagania są dozwolone, ale liczba przydziałów nie "
        "może przekraczać `required_workers`. Każdy przypisany użytkownik "
        "musi mieć rolę `employee`, `is_active=true` i `activated_at` inne "
        "niż null. Nieaktywne stanowiska i potwierdzone historyczne ostrzeżenia "
        "nie blokują publikacji. Publikacja jest nieodwracalna; wakatów nie "
        "można uzupełnić później istniejącym API. Ustawia `published_at` "
        "w UTC. Odpowiedzi zawierają `Cache-Control: no-store`."
    ),
    responses={
        200: _success_response(200),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Active manager required"),
        404: _problem_response("Schedule not found"),
        409: _problem_response(
            "schedule-state-conflict or schedule-publication-blocked. The latter "
            "covers missing requirements, overstaffing and ineligible assignees."
        ),
        422: _problem_response("validation-error", validation=True),
    },
)
def publish_schedule_endpoint(
    schedule_id: ScheduleIdPath,
    response: Response,
    identity: ManagerIdentity,
    body_is_empty: Annotated[None, Depends(_reject_nonempty_body)],
    session: SessionDependency,
) -> ScheduleResponse:
    """Publish a validated schedule and return its committed state."""
    try:
        schedule = publish_schedule(
            session, _parse_schedule_id(schedule_id), actor_id=identity.user_id
        )
    except SchedulePublicationForbidden:
        raise AuthHttpError(
            403, "about:blank", "Odmowa dostępu", "Brak uprawnienia."
        ) from None
    except ScheduleNotFound:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono grafiku", "Grafik nie istnieje."
        ) from None
    except ScheduleStateConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:schedule-state-conflict",
            "Niedozwolona zmiana stanu grafiku",
            "Opublikować można tylko grafik w stanie closed.",
        ) from None
    except SchedulePublicationBlocked as exc:
        details = {
            "no_requirements": "Grafik musi zawierać co najmniej jedno wymaganie.",
            "overstaffed": "Grafik zawiera wymaganie z nadmiarową obsadą.",
            "ineligible_user": (
                "Grafik zawiera przydział nieaktywnego lub nieuprawnionego pracownika."
            ),
        }
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:schedule-publication-blocked",
            "Nie można opublikować grafiku",
            details[exc.reason],
        ) from None
    response.headers["Cache-Control"] = "no-store"
    return _public_schedule(schedule)
