"""HTTP read endpoints for manager shift assignments."""

from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session

from src.assignments.models import ShiftAssignment
from src.assignments.schemas import ShiftAssignmentListResponse, ShiftAssignmentResponse
from src.assignments.service import (
    AssignmentNotFound,
    AssignmentReadForbidden,
    AssignmentScheduleNotFound,
    list_shift_assignments,
    read_shift_assignment,
)
from src.auth.dependencies import AuthHttpError, require_roles
from src.auth.schemas import ProblemDetails, validation_problem_schema
from src.auth.session_service import ActiveIdentity
from src.core.database import get_session
from src.schedules.models import ShiftRequirement

router = APIRouter(prefix="/schedules", tags=["shift-assignments"])
SessionDependency = Annotated[Session, Depends(get_session)]
ReadIdentity = Annotated[ActiveIdentity, Depends(require_roles("admin", "manager"))]
PathId = Annotated[str, Path(pattern=r"^[0-9]+$", max_length=19)]
MAX_BIGINT = 9223372036854775807
ALLOWED_QUERY = {"limit", "offset", "work_date", "shift_requirement_id", "user_id"}


def _problem_response(description: str, *, validation: bool = False) -> dict:
    """Document the project's RFC 9457 response shape."""
    schema = (
        validation_problem_schema()
        if validation
        else ProblemDetails.model_json_schema()
    )
    return {
        "description": description,
        "content": {"application/problem+json": {"schema": schema}},
    }


def _query_error(field: str, kind: str = "value_error") -> None:
    raise RequestValidationError([{"type": kind, "loc": ("query", field)}])


def _parse_decimal(raw: str, field: str, *, minimum: int, maximum: int) -> int:
    """Parse bounded ASCII decimal input without unbounded integer conversion."""
    if not raw or not raw.isascii() or not raw.isdecimal():
        _query_error(field, "int_parsing")
    normalized = raw.lstrip("0") or "0"
    upper = str(maximum)
    if len(normalized) > len(upper) or (
        len(normalized) == len(upper) and normalized > upper
    ):
        _query_error(field, "less_than_equal")
    parsed = int(normalized)
    if parsed < minimum:
        _query_error(field, "greater_than_equal")
    return parsed


def _parse_path_id(raw: str, field: str) -> int:
    """Validate path IDs within PostgreSQL BIGINT range."""
    if not raw.isascii() or not raw.isdecimal():
        raise RequestValidationError([{"type": "int_parsing", "loc": ("path", field)}])
    normalized = raw.lstrip("0") or "0"
    upper = str(MAX_BIGINT)
    if len(normalized) > len(upper) or (
        len(normalized) == len(upper) and normalized > upper
    ):
        raise RequestValidationError([{"type": "value_error", "loc": ("path", field)}])
    return int(normalized)


def _parse_query(
    request: Request,
) -> tuple[int, int, date | None, int | None, int | None]:
    """Reject unknown/repeated filters and parse the documented list query."""
    keys = set(request.query_params.keys())
    invalid = (keys - ALLOWED_QUERY) | {
        key
        for key in keys & ALLOWED_QUERY
        if len(request.query_params.getlist(key)) > 1
    }
    if invalid:
        raise RequestValidationError(
            [
                {"type": "extra_forbidden", "loc": ("query", key)}
                for key in sorted(invalid)
            ]
        )

    values = request.query_params
    limit = (
        _parse_decimal(values["limit"], "limit", minimum=1, maximum=100)
        if "limit" in values
        else 25
    )
    offset = (
        _parse_decimal(values["offset"], "offset", minimum=0, maximum=MAX_BIGINT)
        if "offset" in values
        else 0
    )
    work_date = None
    if "work_date" in values:
        raw = values["work_date"]
        if (
            len(raw) != 10
            or not raw.isascii()
            or raw[4] != "-"
            or raw[7] != "-"
            or not (raw[:4] + raw[5:7] + raw[8:]).isdecimal()
        ):
            _query_error("work_date")
        try:
            work_date = date.fromisoformat(raw)
        except ValueError:
            _query_error("work_date")

    requirement_id = (
        _parse_decimal(
            values["shift_requirement_id"],
            "shift_requirement_id",
            minimum=1,
            maximum=MAX_BIGINT,
        )
        if "shift_requirement_id" in values
        else None
    )
    user_id = (
        _parse_decimal(values["user_id"], "user_id", minimum=1, maximum=MAX_BIGINT)
        if "user_id" in values
        else None
    )
    return limit, offset, work_date, requirement_id, user_id


def _utc(value: datetime | None) -> datetime | None:
    """Represent stored event instants in UTC without changing local shift time."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _public_assignment(
    assignment: ShiftAssignment, requirement: ShiftRequirement
) -> ShiftAssignmentResponse:
    """Build the approved response from one joined row."""
    return ShiftAssignmentResponse(
        id=str(assignment.id),
        schedule_id=str(requirement.schedule_id),
        shift_requirement_id=str(assignment.shift_requirement_id),
        user_id=str(assignment.user_id),
        assigned_by=str(assignment.assigned_by),
        assigned_at=_utc(assignment.assigned_at),
        warning_type=assignment.warning_type,
        warning_acknowledged_at=_utc(assignment.warning_acknowledged_at),
        work_date=requirement.work_date,
        start_time=requirement.start_time,
        end_time=requirement.end_time,
    )


def _query_parameters() -> list[dict[str, object]]:
    """Expose strict manually parsed filters in OpenAPI."""
    return [
        {
            "name": "limit",
            "in": "query",
            "required": False,
            "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
        },
        {
            "name": "offset",
            "in": "query",
            "required": False,
            "schema": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_BIGINT,
                "default": 0,
            },
        },
        {
            "name": "work_date",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "format": "date"},
        },
        {
            "name": "shift_requirement_id",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "pattern": "^[0-9]{1,19}$"},
        },
        {
            "name": "user_id",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "pattern": "^[0-9]{1,19}$"},
        },
    ]


@router.get(
    "/{schedule_id}/shift-assignments",
    response_model=ShiftAssignmentListResponse,
    summary="Pobierz przydziały grafiku",
    description=(
        "Wymaga Bearer JWT i aktualnej roli `manager` lub `admin`; `employee` "
        "otrzymuje 403. Odczyt we wszystkich statusach grafiku. Identyfikatory "
        "ścieżki: 1–19 cyfr ASCII, zakres 0–9223372036854775807; BIGINT w JSON "
        "jest stringiem. Filtry work_date (YYYY-MM-DD), shift_requirement_id "
        "i user_id można łączyć. Limit 1–100, offset 0–BIGINT. Total liczy "
        "wyniki przed paginacją. Sortowanie: work_date, start_time, "
        "shift_requirement_id, assignment id rosnąco. Godziny zmiany są "
        "lokalne Europe/Warsaw, zapisane ostrzeżenie jest historyczne. "
        "Odpowiedź ma Cache-Control: no-store."
    ),
    openapi_extra={"parameters": _query_parameters()},
    responses={
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Manager or admin required"),
        404: _problem_response("Schedule not found"),
        422: _problem_response("validation-error", validation=True),
    },
)
def list_assignments(
    schedule_id: PathId,
    request: Request,
    response: Response,
    identity: ReadIdentity,
    session: SessionDependency,
) -> ShiftAssignmentListResponse:
    """Return one schedule-scoped filtered page."""
    parsed_schedule_id = _parse_path_id(schedule_id, "schedule_id")
    limit, offset, work_date, requirement_id, user_id = _parse_query(request)
    try:
        rows, total = list_shift_assignments(
            session,
            parsed_schedule_id,
            role=identity.role,
            work_date=work_date,
            shift_requirement_id=requirement_id,
            user_id=user_id,
            limit=limit,
            offset=offset,
        )
    except AssignmentReadForbidden:
        raise AuthHttpError(
            403, "about:blank", "Odmowa dostępu", "Brak uprawnienia."
        ) from None
    except AssignmentScheduleNotFound:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono grafiku", "Grafik nie istnieje."
        ) from None
    response.headers["Cache-Control"] = "no-store"
    return ShiftAssignmentListResponse(
        items=[
            _public_assignment(assignment, requirement)
            for assignment, requirement in rows
        ],
        limit=limit,
        offset=offset,
        total=total,
    )


@router.get(
    "/{schedule_id}/shift-assignments/{assignment_id}",
    response_model=ShiftAssignmentResponse,
    summary="Pobierz szczegóły przydziału",
    description=(
        "Wymaga Bearer JWT i aktualnej roli `manager` lub `admin`; `employee` "
        "otrzymuje 403. Odczyt we wszystkich statusach grafiku. Obcy przydział "
        "daje 404. Identyfikatory: 1–19 cyfr ASCII i zakres BIGINT. Zwraca "
        "historyczne warning_type; assigned_at i warning_acknowledged_at są UTC "
        "RFC 3339, godziny zmiany są lokalne Europe/Warsaw. "
        "Odpowiedź ma Cache-Control: no-store."
    ),
    responses={
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Manager or admin required"),
        404: _problem_response("Schedule or assignment not found"),
        422: _problem_response("validation-error", validation=True),
    },
)
def get_assignment(
    schedule_id: PathId,
    assignment_id: PathId,
    response: Response,
    identity: ReadIdentity,
    session: SessionDependency,
) -> ShiftAssignmentResponse:
    """Return one assignment through its schedule's requirement."""
    parsed_schedule_id = _parse_path_id(schedule_id, "schedule_id")
    parsed_assignment_id = _parse_path_id(assignment_id, "assignment_id")
    try:
        assignment, requirement = read_shift_assignment(
            session, parsed_schedule_id, parsed_assignment_id, role=identity.role
        )
    except AssignmentReadForbidden:
        raise AuthHttpError(
            403, "about:blank", "Odmowa dostępu", "Brak uprawnienia."
        ) from None
    except (AssignmentScheduleNotFound, AssignmentNotFound):
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono przydziału", "Zasób nie istnieje."
        ) from None
    response.headers["Cache-Control"] = "no-store"
    return _public_assignment(assignment, requirement)
