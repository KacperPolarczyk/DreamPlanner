"""HTTP endpoints for manager shift assignments."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session

from src.assignments.schemas import (
    CreateShiftAssignmentInput,
    MyShiftListResponse,
    MyShiftResponse,
    ShiftAssignmentListResponse,
    ShiftAssignmentResponse,
)
from src.assignments.service import (
    AssignmentAlreadyExists,
    AssignmentCapacityExceeded,
    AssignmentNotFound,
    AssignmentOverlap,
    AssignmentReadForbidden,
    AssignmentRequirementNotFound,
    AssignmentScheduleNotFound,
    AssignmentScheduleStateConflict,
    AssignmentUserIneligible,
    AssignmentUserNotFound,
    AssignmentWarningRequired,
    AssignmentWriteForbidden,
    assignment_response,
    create_shift_assignment,
    delete_shift_assignment,
    list_my_published_shifts,
    list_shift_assignments,
    my_shift_response,
    read_my_published_shift,
    read_shift_assignment,
)
from src.auth.dependencies import (
    AuthHttpError,
    require_active_employee,
    require_manager_write,
    require_roles,
)
from src.auth.schemas import ProblemDetails, validation_problem_schema
from src.auth.session_service import ActiveIdentity
from src.core.database import get_session

router = APIRouter(prefix="/schedules", tags=["shift-assignments"])
employee_router = APIRouter(prefix="/me", tags=["my-shifts"])
SessionDependency = Annotated[Session, Depends(get_session)]
ReadIdentity = Annotated[ActiveIdentity, Depends(require_roles("admin", "manager"))]
ManagerIdentity = Annotated[ActiveIdentity, Depends(require_manager_write)]
EmployeeIdentity = Annotated[ActiveIdentity, Depends(require_active_employee)]
PathId = Annotated[str, Path(pattern=r"^[0-9]+$", max_length=19)]
MAX_BIGINT = 9223372036854775807
ALLOWED_QUERY = {"limit", "offset", "work_date", "shift_requirement_id", "user_id"}
MY_SHIFTS_QUERY = {"from_date", "to_date", "limit", "offset"}
_NO_STORE_HEADER = {
    "Cache-Control": {
        "description": "Protected responses are not cacheable.",
        "schema": {"type": "string", "const": "no-store"},
    }
}


def _success_response(status: int) -> dict[str, object]:
    """Document headers returned by successful assignment operations."""
    headers = dict(_NO_STORE_HEADER)
    if status == 201:
        headers["Location"] = {
            "description": "Canonical path of the created assignment.",
            "schema": {"type": "string"},
        }
    return {
        "description": "No Content" if status == 204 else "Successful Response",
        "headers": headers,
    }


async def _reject_nonempty_body(request: Request) -> None:
    """Reject content on the bodyless assignment deletion operation."""
    if await request.body():
        raise RequestValidationError([{"type": "extra_forbidden", "loc": ("body",)}])


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
        "headers": _NO_STORE_HEADER,
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


def _reject_unexpected_query(request: Request) -> None:
    """Reject query parameters on routes that define none."""
    if request.query_params:
        raise RequestValidationError([{"type": "extra_forbidden", "loc": ("query",)}])


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


def _my_shifts_query_parameters() -> list[dict[str, object]]:
    """Describe the supported date range and pagination filters."""
    return [
        {
            "name": "from_date",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "format": "date"},
        },
        {
            "name": "to_date",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "format": "date"},
        },
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
    ]


def _parse_my_shifts_query(
    request: Request,
) -> tuple[int, int, date | None, date | None]:
    """Validate only the documented inclusive date filters and page values."""
    values = request.query_params
    invalid = (set(values) - MY_SHIFTS_QUERY) | {
        key for key in set(values) & MY_SHIFTS_QUERY if len(values.getlist(key)) > 1
    }
    if invalid:
        raise RequestValidationError(
            [
                {"type": "extra_forbidden", "loc": ("query", key)}
                for key in sorted(invalid)
            ]
        )

    parsed_dates: list[date | None] = []
    for key in ("from_date", "to_date"):
        raw = values.get(key)
        if raw is None:
            parsed_dates.append(None)
            continue
        if (
            len(raw) != 10
            or not raw.isascii()
            or raw[4] != "-"
            or raw[7] != "-"
            or not (raw[:4] + raw[5:7] + raw[8:]).isdecimal()
        ):
            _query_error(key, "date_from_datetime_parsing")
        try:
            parsed_dates.append(date.fromisoformat(raw))
        except ValueError:
            _query_error(key, "date_from_datetime_parsing")

    from_date, to_date = parsed_dates
    if from_date is not None and to_date is not None and from_date > to_date:
        _query_error("to_date")
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
    return limit, offset, from_date, to_date


@employee_router.get(
    "/shifts",
    response_model=MyShiftListResponse,
    summary="Pobierz własne opublikowane zmiany",
    description=(
        "Wymaga aktywnej sesji i aktualnej roli aktywnego employee. Zwraca "
        "wyłącznie własne przydziały z grafików `published`; identyfikator "
        "użytkownika pochodzi z uwierzytelnionej tożsamości. Daty `from_date` "
        "i `to_date` są włączne. Sortowanie: `work_date`, `start_time`, "
        "`shift_requirement_id`, `assignment_id` rosnąco. `total` uwzględnia "
        "te same filtry przed paginacją. Godziny zmian są lokalne "
        "`Europe/Warsaw`, a ostrzeżenia pozostają historyczne. Odpowiedź "
        "zawiera `Cache-Control: no-store`."
    ),
    openapi_extra={"parameters": _my_shifts_query_parameters()},
    responses={
        200: _success_response(200),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Active employee required"),
        422: _problem_response("validation-error", validation=True),
    },
)
def list_my_shifts(
    request: Request,
    response: Response,
    identity: EmployeeIdentity,
    session: SessionDependency,
) -> MyShiftListResponse:
    """Return one bounded page scoped to the current employee."""
    limit, offset, from_date, to_date = _parse_my_shifts_query(request)
    rows, total = list_my_published_shifts(
        session,
        identity.user_id,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        offset=offset,
    )
    response.headers["Cache-Control"] = "no-store"
    return MyShiftListResponse(
        items=[
            my_shift_response(assignment, requirement, position_name)
            for assignment, requirement, position_name in rows
        ],
        limit=limit,
        offset=offset,
        total=total,
    )


@employee_router.get(
    "/shifts/{assignment_id}",
    response_model=MyShiftResponse,
    summary="Pobierz własną opublikowaną zmianę",
    description=(
        "Wymaga aktywnej sesji i aktualnej roli aktywnego employee. Przydział "
        "musi należeć do uwierzytelnionego użytkownika i grafiku o statusie "
        "`published`; brak dostępu, cudzy, nieopublikowany lub nieznany ID "
        "zwraca ten sam `404`. ID jest BIGINT. Godziny zmiany są lokalne "
        "`Europe/Warsaw`, a ostrzeżenie jest historyczne. Odpowiedź zawiera "
        "`Cache-Control: no-store`."
    ),
    responses={
        200: _success_response(200),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Active employee required"),
        404: _problem_response("Assignment not found"),
        422: _problem_response("validation-error", validation=True),
    },
)
def get_my_shift(
    assignment_id: PathId,
    request: Request,
    response: Response,
    identity: EmployeeIdentity,
    session: SessionDependency,
) -> MyShiftResponse:
    """Return an own published assignment without revealing hidden IDs."""
    parsed_assignment_id = _parse_path_id(assignment_id, "assignment_id")
    _reject_unexpected_query(request)
    row = read_my_published_shift(session, identity.user_id, parsed_assignment_id)
    if row is None:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono przydziału", "Zasób nie istnieje."
        )
    response.headers["Cache-Control"] = "no-store"
    assignment, requirement, position_name = row
    return my_shift_response(assignment, requirement, position_name)


@router.get(
    "/{schedule_id}/shift-assignments",
    response_model=ShiftAssignmentListResponse,
    summary="Pobierz przydziały grafiku",
    description=(
        "Wymaga Bearer JWT i aktualnej roli `manager` lub `admin`; `employee` "
        "otrzymuje 403. Odczyt we wszystkich statusach grafiku. Identyfikatory "
        "ścieżki: 1-19 cyfr ASCII, zakres 0-9223372036854775807; BIGINT w JSON "
        "jest stringiem. Filtry work_date (YYYY-MM-DD), shift_requirement_id "
        "i user_id można łączyć. Limit 1-100, offset 0-BIGINT. `total` liczy "
        "wyniki przed paginacją. Sortowanie: work_date, start_time, "
        "shift_requirement_id, assignment id rosnąco. Godziny zmiany są "
        "lokalne Europe/Warsaw, zapisane ostrzeżenie jest historyczne. "
        "Odpowiedź ma Cache-Control: no-store."
    ),
    openapi_extra={"parameters": _query_parameters()},
    responses={
        200: _success_response(200),
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
            assignment_response(assignment, requirement)
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
        "daje 404. Identyfikatory: 1-19 cyfr ASCII i zakres BIGINT. Zwraca "
        "historyczne warning_type; assigned_at i warning_acknowledged_at są UTC "
        "RFC 3339, godziny zmiany są lokalne Europe/Warsaw. "
        "Odpowiedź ma Cache-Control: no-store."
    ),
    responses={
        200: _success_response(200),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Manager or admin required"),
        404: _problem_response("Schedule or assignment not found"),
        422: _problem_response("validation-error", validation=True),
    },
)
def get_assignment(
    schedule_id: PathId,
    assignment_id: PathId,
    request: Request,
    response: Response,
    identity: ReadIdentity,
    session: SessionDependency,
) -> ShiftAssignmentResponse:
    """Return one assignment through its schedule's requirement."""
    parsed_schedule_id = _parse_path_id(schedule_id, "schedule_id")
    parsed_assignment_id = _parse_path_id(assignment_id, "assignment_id")
    _reject_unexpected_query(request)
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
    return assignment_response(assignment, requirement)


@router.post(
    "/{schedule_id}/shift-assignments",
    status_code=201,
    response_model=ShiftAssignmentResponse,
    summary="Przypisz pracownika do zmiany",
    description=(
        "Wymaga Bearer JWT i aktualnej roli `manager`; `admin` i `employee` "
        "otrzymują 403. Grafik musi być w stanie `closed`. Treść żądania wymaga "
        "dodatnich dziesiętnych stringów BIGINT dla shift_requirement_id i "
        "user_id; warning_acknowledged jest opcjonalnym boolean (domyślnie "
        "false). Wymagany jest aktywny, aktywowany employee. Nieaktywne "
        "stanowisko nie blokuje przydziału. Liczba przydziałów nie może "
        "przekroczyć required_workers, a zmiany pracownika nie mogą się "
        "nakładać. Oceniane są tylko przesłane deklaracje dyspozycyjności: "
        "unavailable ma pierwszeństwo, pełne pokrycie available nie wymaga "
        "ostrzeżenia, pozostałe przypadki to unknown. Dla unavailable i "
        "unknown wymagane jest jawne potwierdzenie. Daty i godziny zmiany "
        "są lokalne Europe/Warsaw; timestampy audytu są UTC RFC 3339. "
        "Sukces zwraca Location i Cache-Control: no-store."
    ),
    responses={
        201: _success_response(201),
        400: _problem_response("Malformed JSON"),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Manager required"),
        404: _problem_response("Schedule, requirement or user not found"),
        409: _problem_response(
            "schedule-state-conflict, assignment-user-ineligible, "
            "assignment-capacity-exceeded, assignment-already-exists, "
            "assignment-overlap or assignment-warning-required"
        ),
        422: _problem_response("validation-error", validation=True),
    },
)
def post_assignment(
    schedule_id: PathId,
    data: CreateShiftAssignmentInput,
    request: Request,
    response: Response,
    identity: ManagerIdentity,
    session: SessionDependency,
) -> ShiftAssignmentResponse:
    """Create and return one committed assignment with a canonical Location."""
    parsed_schedule_id = _parse_path_id(schedule_id, "schedule_id")
    _reject_unexpected_query(request)
    try:
        result = create_shift_assignment(
            session,
            parsed_schedule_id,
            actor_id=identity.user_id,
            actor_role=identity.role,
            requirement_id=int(data.shift_requirement_id),
            user_id=int(data.user_id),
            warning_acknowledged=data.warning_acknowledged,
        )
    except AssignmentWriteForbidden:
        raise AuthHttpError(
            403, "about:blank", "Odmowa dostępu", "Brak uprawnienia."
        ) from None
    except (
        AssignmentScheduleNotFound,
        AssignmentRequirementNotFound,
        AssignmentUserNotFound,
    ):
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono zasobu", "Zasób nie istnieje."
        ) from None
    except AssignmentScheduleStateConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:schedule-state-conflict",
            "Grafik nie pozwala na tę operację",
            "Przydziały można tworzyć tylko w grafiku closed.",
        ) from None
    except AssignmentUserIneligible:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:assignment-user-ineligible",
            "Nie można przypisać użytkownika",
            "Użytkownik nie może otrzymać przydziału.",
        ) from None
    except AssignmentCapacityExceeded:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:assignment-capacity-exceeded",
            "Brak wolnego miejsca",
            "Wymagana obsada została osiągnięta.",
        ) from None
    except AssignmentAlreadyExists:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:assignment-already-exists",
            "Przydział już istnieje",
            "Pracownik jest już przypisany do tej zmiany.",
        ) from None
    except AssignmentOverlap:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:assignment-overlap",
            "Kolizja przydziałów",
            "Pracownik ma nakładający się przydział.",
        ) from None
    except AssignmentWarningRequired:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:assignment-warning-required",
            "Wymagane potwierdzenie",
            "Przydział wymaga potwierdzenia ostrzeżenia.",
        ) from None

    response.headers["Location"] = (
        f"/api/v1/schedules/{parsed_schedule_id}/shift-assignments/{result.id}"
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.delete(
    "/{schedule_id}/shift-assignments/{assignment_id}",
    status_code=204,
    summary="Usuń przydział do zmiany",
    description=(
        "Wymaga Bearer JWT i aktualnej roli `manager`; `admin` i `employee` "
        "otrzymują 403. Grafik musi być w stanie `closed`; DELETE nie zmienia "
        "jego statusu ani required_workers. Przydział musi należeć do grafiku; "
        "obcy lub ponownie usuwany przydział daje 404. Nie sprawdza bieżącej "
        "aktywności pracownika ani stanowiska i nie przelicza ostrzeżeń. "
        "Identyfikatory: 1-19 cyfr ASCII, zakres 0-9223372036854775807. "
        "Niepuste body, także {} lub [], daje 422. Sukces 204 nie ma treści "
        "i zawiera Cache-Control: no-store."
    ),
    responses={
        204: _success_response(204),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Manager required"),
        404: _problem_response("Schedule or assignment not found"),
        409: _problem_response("schedule-state-conflict"),
        422: _problem_response("validation-error", validation=True),
    },
)
def delete_assignment(
    schedule_id: PathId,
    assignment_id: PathId,
    request: Request,
    identity: ManagerIdentity,
    body_is_empty: Annotated[None, Depends(_reject_nonempty_body)],
    session: SessionDependency,
) -> Response:
    """Delete one assignment in the requested closed schedule."""
    parsed_schedule_id = _parse_path_id(schedule_id, "schedule_id")
    parsed_assignment_id = _parse_path_id(assignment_id, "assignment_id")
    _reject_unexpected_query(request)
    try:
        delete_shift_assignment(
            session, parsed_schedule_id, parsed_assignment_id, actor_role=identity.role
        )
    except AssignmentWriteForbidden:
        raise AuthHttpError(
            403, "about:blank", "Odmowa dostępu", "Brak uprawnienia."
        ) from None
    except (AssignmentScheduleNotFound, AssignmentNotFound):
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono przydziału", "Zasób nie istnieje."
        ) from None
    except AssignmentScheduleStateConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:schedule-state-conflict",
            "Grafik nie pozwala na tę operację",
            "Przydziały można usuwać tylko w grafiku closed.",
        ) from None

    return Response(status_code=204, headers={"Cache-Control": "no-store"})
