"""HTTP endpoints for reading monthly availability submissions."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session

from src.auth.dependencies import AuthHttpError, require_roles
from src.auth.schemas import ProblemDetails, validation_problem_schema
from src.auth.session_service import ActiveIdentity
from src.availability.models import AvailabilityEntry, AvailabilitySubmission
from src.availability.schemas import (
    AvailabilityEntryResponse,
    AvailabilitySubmissionListResponse,
    AvailabilitySubmissionResponse,
    AvailabilitySubmissionSummary,
    CreateAvailabilitySubmissionInput,
    UpdateAvailabilitySubmissionInput,
)
from src.availability.service import (
    AvailabilityEntryConflict,
    AvailabilityScheduleNotFound,
    AvailabilityScheduleStateConflict,
    AvailabilitySubmissionConflict,
    AvailabilitySubmissionNotFound,
)
from src.availability.service import (
    create_availability_submission as create_submission,
)
from src.availability.service import (
    list_availability_submissions as list_submissions,
)
from src.availability.service import (
    read_availability_submission as read_submission,
)
from src.availability.service import (
    update_availability_submission as update_submission,
)
from src.core.database import get_session

router = APIRouter(prefix="/schedules", tags=["availability-submissions"])
SessionDependency = Annotated[Session, Depends(get_session)]
ReadIdentity = Annotated[
    ActiveIdentity, Depends(require_roles("admin", "manager", "employee"))
]
PathId = Annotated[str, Path(pattern=r"^[0-9]+$", max_length=19)]
MAX_BIGINT = 9223372036854775807
ALLOWED_QUERY = {"limit", "offset", "user_id", "state"}
EmployeeIdentity = Annotated[ActiveIdentity, Depends(require_roles("employee"))]


def _problem_response(description: str, *, validation: bool = False):
    """Describe an existing RFC 9457 response shape in OpenAPI."""
    schema = (
        validation_problem_schema()
        if validation
        else ProblemDetails.model_json_schema()
    )
    return {
        "description": description,
        "content": {"application/problem+json": {"schema": schema}},
    }


def _parse_path_id(value: str, field: str) -> int:
    """Validate an ASCII decimal BIGINT path identifier."""
    if not value.isascii() or not value.isdecimal():
        raise RequestValidationError([{"type": "int_parsing", "loc": ("path", field)}])
    parsed = int(value)
    if parsed > MAX_BIGINT:
        raise RequestValidationError([{"type": "value_error", "loc": ("path", field)}])
    return parsed


def _query_error(field: str, error_type: str = "value_error") -> None:
    raise RequestValidationError([{"type": error_type, "loc": ("query", field)}])


def _bounded_decimal(raw: str, field: str, minimum: int, maximum: int) -> int:
    """Parse an ASCII decimal integer without converting unbounded input."""
    if not raw or not raw.isascii() or not raw.isdecimal():
        _query_error(field, "int_parsing")
    normalized = raw.lstrip("0") or "0"
    maximum_text = str(maximum)
    if len(normalized) > len(maximum_text) or (
        len(normalized) == len(maximum_text) and normalized > maximum_text
    ):
        _query_error(field, "less_than_equal")
    parsed = int(normalized)
    if parsed < minimum:
        _query_error(field, "greater_than_equal")
    return parsed


def _parse_list_query(
    request: Request, role: str
) -> tuple[int, int, int | None, Literal["draft", "submitted"] | None]:
    """Parse only the documented query names with strict decimal syntax."""
    keys = set(request.query_params.keys())
    if role == "employee" and "user_id" in keys:
        raise AuthHttpError(403, "about:blank", "Odmowa dostępu", "Niedozwolony filtr.")

    unknown = keys - ALLOWED_QUERY
    repeated = {
        key
        for key in keys & ALLOWED_QUERY
        if len(request.query_params.getlist(key)) > 1
    }
    if unknown or repeated:
        raise RequestValidationError(
            [
                {"type": "extra_forbidden", "loc": ("query", field)}
                for field in sorted(unknown | repeated)
            ]
        )

    values = {key: request.query_params.get(key) for key in ALLOWED_QUERY}
    numeric: dict[str, int] = {}
    for field, default, minimum, maximum in (
        ("limit", 25, 1, 100),
        ("offset", 0, 0, MAX_BIGINT),
    ):
        raw = values[field]
        if raw is None:
            numeric[field] = default
            continue
        numeric[field] = _bounded_decimal(raw, field, minimum, maximum)

    user_id = None
    raw_user_id = values["user_id"]
    if raw_user_id is not None:
        if (
            not raw_user_id
            or len(raw_user_id) > 19
            or not raw_user_id.isascii()
            or not raw_user_id.isdecimal()
        ):
            _query_error("user_id", "string_pattern_mismatch")
        user_id = int(raw_user_id)
        if not 0 < user_id <= MAX_BIGINT:
            _query_error("user_id")

    state_value = values["state"]
    if state_value not in (None, "draft", "submitted"):
        _query_error("state", "literal_error")

    return (
        numeric["limit"],
        numeric["offset"],
        user_id,
        state_value,
    )


def _utc(value: datetime | None) -> datetime | None:
    """Serialize database event timestamps as UTC RFC 3339 values."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _summary(submission: AvailabilitySubmission) -> AvailabilitySubmissionSummary:
    """Select the public metadata fields of a submission."""
    return AvailabilitySubmissionSummary(
        id=str(submission.id),
        schedule_id=str(submission.schedule_id),
        user_id=str(submission.user_id),
        submitted_at=_utc(submission.submitted_at),
        created_at=_utc(submission.created_at),
        updated_at=_utc(submission.updated_at),
    )


def _entry(entry: AvailabilityEntry) -> AvailabilityEntryResponse:
    """Select public entry fields without exposing the ORM object."""
    return AvailabilityEntryResponse(
        id=str(entry.id),
        work_date=entry.work_date,
        start_time=entry.start_time,
        end_time=entry.end_time,
        status=entry.status,
        note=entry.note,
    )


def _openapi_query_parameters() -> list[dict[str, object]]:
    """Return explicit query parameter schemas for strict manual parsing."""
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
            "name": "user_id",
            "in": "query",
            "required": False,
            "description": "Dostępny wyłącznie dla ról manager i admin.",
            "schema": {"type": "string", "pattern": "^[0-9]{1,19}$"},
        },
        {
            "name": "state",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "enum": ["draft", "submitted"]},
        },
    ]


@router.get(
    "/{schedule_id}/availability-submissions",
    response_model=AvailabilitySubmissionListResponse,
    summary="Pobierz deklaracje dyspozycyjności grafiku",
    description=(
        "Wymaga Bearer JWT i aktualnej roli employee, manager lub admin. "
        "Employee widzi wyłącznie własną deklarację; manager i admin widzą "
        "wszystkie. Odczyt działa w każdym stanie grafiku. Lista nie zawiera "
        "entries i używa stałego sortowania user_id ASC, id ASC. Filtr "
        "user_id jest dostępny wyłącznie managerowi i adminowi; employee "
        "otrzymuje 403. Chroniona odpowiedź ma Cache-Control: no-store."
    ),
    openapi_extra={"parameters": _openapi_query_parameters()},
    responses={
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Role or filter is forbidden"),
        404: _problem_response("Schedule not found"),
        422: _problem_response("validation-error", validation=True),
    },
)
def list_availability_submissions(
    schedule_id: PathId,
    request: Request,
    response: Response,
    identity: ReadIdentity,
    session: SessionDependency,
) -> AvailabilitySubmissionListResponse:
    """Return a page restricted by current role and employee ownership."""
    parsed_schedule_id = _parse_path_id(schedule_id, "schedule_id")
    limit, offset, user_id, state = _parse_list_query(request, identity.role)
    try:
        submissions, total = list_submissions(
            session,
            parsed_schedule_id,
            role=identity.role,
            authenticated_user_id=identity.user_id,
            user_id=user_id,
            state=state,
            limit=limit,
            offset=offset,
        )
    except AvailabilityScheduleNotFound:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono grafiku", "Grafik nie istnieje."
        ) from None

    response.headers["Cache-Control"] = "no-store"
    return AvailabilitySubmissionListResponse(
        items=[_summary(submission) for submission in submissions],
        limit=limit,
        offset=offset,
        total=total,
    )


@router.get(
    "/{schedule_id}/availability-submissions/{submission_id}",
    response_model=AvailabilitySubmissionResponse,
    summary="Pobierz szczegóły deklaracji dyspozycyjności",
    description=(
        "Wymaga Bearer JWT i aktualnej roli employee, manager lub admin. "
        "Employee może odczytać wyłącznie własny zasób; cudza i nieistniejąca "
        "deklaracja dają ten sam 404. Odczyt działa w każdym stanie grafiku. "
        "Entries są sortowane po work_date, start_time i id. Odpowiedź ma "
        "Cache-Control: no-store."
    ),
    responses={
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Role is forbidden"),
        404: _problem_response("Schedule or visible submission not found"),
        422: _problem_response("validation-error", validation=True),
    },
)
def read_availability_submission(
    schedule_id: PathId,
    submission_id: Annotated[str, Path(pattern=r"^[0-9]+$", max_length=19)],
    response: Response,
    identity: ReadIdentity,
    session: SessionDependency,
) -> AvailabilitySubmissionResponse:
    """Return one scoped submission and its ordered entries."""
    parsed_schedule_id = _parse_path_id(schedule_id, "schedule_id")
    parsed_submission_id = _parse_path_id(submission_id, "submission_id")
    try:
        submission, entries = read_submission(
            session,
            parsed_schedule_id,
            parsed_submission_id,
            role=identity.role,
            authenticated_user_id=identity.user_id,
        )
    except AvailabilityScheduleNotFound:
        raise AuthHttpError(
            404,
            "about:blank",
            "Nie znaleziono deklaracji",
            "Zasób nie istnieje.",
        ) from None
    except AvailabilitySubmissionNotFound:
        raise AuthHttpError(
            404,
            "about:blank",
            "Nie znaleziono deklaracji",
            "Zasób nie istnieje.",
        ) from None

    response.headers["Cache-Control"] = "no-store"
    return AvailabilitySubmissionResponse(
        **_summary(submission).model_dump(),
        entries=[_entry(entry) for entry in entries],
    )


@router.post(
    "/{schedule_id}/availability-submissions",
    response_model=AvailabilitySubmissionResponse,
    status_code=201,
    summary="Utwórz własną deklarację dyspozycyjności",
    description=(
        "Dostępne wyłącznie dla aktywnego employee. Tworzy jedną własną "
        "deklarację dla grafiku w stanie collecting. Odpowiedź po commit ma "
        "Cache-Control: no-store i Location."
    ),
    responses={
        400: _problem_response("Malformed JSON"),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Employee role required"),
        404: _problem_response("Schedule not found"),
        409: _problem_response("Schedule state or declaration conflict"),
        422: _problem_response("validation-error", validation=True),
    },
)
def create_availability_submission(
    schedule_id: PathId,
    payload: CreateAvailabilitySubmissionInput,
    response: Response,
    identity: EmployeeIdentity,
    session: SessionDependency,
) -> AvailabilitySubmissionResponse:
    """Create the authenticated employee's one monthly declaration."""
    parsed_schedule_id = _parse_path_id(schedule_id, "schedule_id")
    try:
        result = create_submission(
            session,
            parsed_schedule_id,
            identity.user_id,
            entries=payload.entries,
            submit=payload.submit,
        )
    except AvailabilityScheduleNotFound:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono grafiku", "Grafik nie istnieje."
        ) from None
    except AvailabilityScheduleStateConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:schedule-state-conflict",
            "Grafik nie pozwala na tę operację",
            "Operacja jest niedostępna w bieżącym stanie grafiku.",
        ) from None
    except AvailabilitySubmissionConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:availability-submission-conflict",
            "Deklaracja już istnieje",
            "Pracownik ma już deklarację dla tego grafiku.",
        ) from None
    except ValueError:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("body", "entries")}]
        ) from None

    response.headers["Location"] = (
        f"/api/v1/schedules/{parsed_schedule_id}/availability-submissions/{result.id}"
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.patch(
    "/{schedule_id}/availability-submissions/{submission_id}",
    response_model=AvailabilitySubmissionResponse,
    summary="Edytuj lub prześlij własną deklarację dyspozycyjności",
    description=(
        "Dostępne wyłącznie dla aktywnego employee i własnej deklaracji. "
        "Pole entries, jeśli podane, zastępuje całą listę. Odpowiedź zawiera "
        "pełny zasób i Cache-Control: no-store."
    ),
    responses={
        400: _problem_response("Malformed JSON"),
        401: _problem_response("Invalid access token or session"),
        403: _problem_response("Employee role required"),
        404: _problem_response("Schedule or visible submission not found"),
        409: _problem_response("Schedule state or availability entry conflict"),
        422: _problem_response("validation-error", validation=True),
    },
)
def update_availability_submission(
    schedule_id: PathId,
    submission_id: Annotated[str, Path(pattern=r"^[0-9]+$", max_length=19)],
    payload: UpdateAvailabilitySubmissionInput,
    response: Response,
    identity: EmployeeIdentity,
    session: SessionDependency,
) -> AvailabilitySubmissionResponse:
    """Replace the employee's entries or submit the current declaration."""
    parsed_schedule_id = _parse_path_id(schedule_id, "schedule_id")
    parsed_submission_id = _parse_path_id(submission_id, "submission_id")
    try:
        result = update_submission(
            session,
            parsed_schedule_id,
            parsed_submission_id,
            identity.user_id,
            changes=payload,
        )
    except AvailabilityScheduleNotFound:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono deklaracji", "Zasób nie istnieje."
        ) from None
    except AvailabilitySubmissionNotFound:
        raise AuthHttpError(
            404, "about:blank", "Nie znaleziono deklaracji", "Zasób nie istnieje."
        ) from None
    except AvailabilityScheduleStateConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:schedule-state-conflict",
            "Grafik nie pozwala na tę operację",
            "Operacja jest niedostępna w bieżącym stanie grafiku.",
        ) from None
    except AvailabilityEntryConflict:
        raise AuthHttpError(
            409,
            "urn:dreamplanner:problem:availability-entry-overlap",
            "Konflikt wpisów dyspozycyjności",
            "Nowe wpisy kolidują z danymi zapisanymi w bazie.",
        ) from None
    except ValueError:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("body", "entries")}]
        ) from None

    response.headers["Cache-Control"] = "no-store"
    return result
