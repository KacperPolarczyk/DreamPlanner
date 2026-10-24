"""Application workflows for schedule-scoped shift assignments."""

from datetime import UTC, date, datetime
from typing import Literal

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from src.assignments.models import ShiftAssignment
from src.assignments.repository import ShiftAssignmentRepository
from src.assignments.schemas import ShiftAssignmentResponse
from src.availability.models import AvailabilityEntry
from src.schedules.models import ShiftRequirement
from src.schedules.repository import ScheduleRepository


class AssignmentReadForbidden(Exception):
    """The caller's current role does not grant manager assignment reads."""


class AssignmentScheduleNotFound(Exception):
    """The requested schedule does not exist."""


class AssignmentNotFound(Exception):
    """The assignment does not belong to the requested schedule."""


class AssignmentWriteForbidden(Exception):
    """The caller is not a manager."""


class AssignmentScheduleStateConflict(Exception):
    """The schedule is not in the staffing stage."""


class AssignmentRequirementNotFound(Exception):
    """The requirement does not belong to the requested schedule."""


class AssignmentUserNotFound(Exception):
    """The target account does not exist."""


class AssignmentUserIneligible(Exception):
    """The target account is not an active, activated employee."""


class AssignmentCapacityExceeded(Exception):
    """The requirement has no unoccupied places."""


class AssignmentAlreadyExists(Exception):
    """The employee is already assigned to this requirement."""


class AssignmentOverlap(Exception):
    """The employee has an overlapping assignment."""


class AssignmentWarningRequired(Exception):
    """The manager did not acknowledge an unavailable or unknown interval."""


def _utc(value: datetime | None) -> datetime | None:
    """Represent stored event instants in UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def assignment_response(
    assignment: ShiftAssignment, requirement: ShiftRequirement
) -> ShiftAssignmentResponse:
    """Materialize an assignment response before its transaction closes."""
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


def _availability_status(
    requirement: ShiftRequirement, entries: list[AvailabilityEntry]
) -> Literal["available", "unavailable", "unknown"]:
    """Classify full half-open shift coverage from submitted intervals."""
    start = requirement.start_time
    end = requirement.end_time
    if any(
        entry.status == "unavailable"
        and entry.start_time < end
        and start < entry.end_time
        for entry in entries
    ):
        return "unavailable"

    covered_until = start
    for entry in entries:
        if entry.status != "available" or entry.end_time <= covered_until:
            continue
        if entry.start_time > covered_until:
            return "unknown"
        covered_until = entry.end_time
        if covered_until >= end:
            return "available"
    return "unknown"


def _assignment_db_conflict(exc: DBAPIError) -> type[Exception] | None:
    """Recognize only the approved PostgreSQL UNIQUE and overlap errors."""
    original = exc.orig
    diagnostic = getattr(original, "diag", None)
    state = getattr(original, "sqlstate", None)
    if (
        state == "23505"
        and getattr(diagnostic, "constraint_name", None) == "uq_shift_assignment"
    ):
        return AssignmentAlreadyExists
    if (
        state == "P0001"
        and getattr(diagnostic, "message_primary", None)
        == "Pracownik ma kolidujacy przydzial"
    ):
        return AssignmentOverlap
    return None


def _require_read_access(role: str) -> None:
    if role not in {"admin", "manager"}:
        raise AssignmentReadForbidden


def list_shift_assignments(
    session: Session,
    schedule_id: int,
    *,
    role: str,
    work_date: date | None,
    shift_requirement_id: int | None,
    user_id: int | None,
    limit: int,
    offset: int,
) -> tuple[list[tuple[ShiftAssignment, ShiftRequirement]], int]:
    """Return one visible page for an existing schedule in any status."""
    _require_read_access(role)
    if ScheduleRepository(session).get_by_id(schedule_id) is None:
        raise AssignmentScheduleNotFound
    return ShiftAssignmentRepository(session).list_for_schedule(
        schedule_id,
        work_date=work_date,
        shift_requirement_id=shift_requirement_id,
        user_id=user_id,
        limit=limit,
        offset=offset,
    )


def read_shift_assignment(
    session: Session, schedule_id: int, assignment_id: int, *, role: str
) -> tuple[ShiftAssignment, ShiftRequirement]:
    """Return one assignment only within the requested schedule."""
    _require_read_access(role)
    if ScheduleRepository(session).get_by_id(schedule_id) is None:
        raise AssignmentScheduleNotFound
    row = ShiftAssignmentRepository(session).get_for_schedule(
        schedule_id, assignment_id
    )
    if row is None:
        raise AssignmentNotFound
    return row


def create_shift_assignment(
    session: Session,
    schedule_id: int,
    *,
    actor_id: int,
    actor_role: str,
    requirement_id: int,
    user_id: int,
    warning_acknowledged: bool,
) -> ShiftAssignmentResponse:
    """Validate and commit one manager assignment under the schedule row lock."""
    try:
        if actor_role != "manager":
            raise AssignmentWriteForbidden

        schedule = ScheduleRepository(session).get_by_id_for_update(schedule_id)
        if schedule is None:
            raise AssignmentScheduleNotFound
        if schedule.status != "closed":
            raise AssignmentScheduleStateConflict

        repository = ShiftAssignmentRepository(session)
        requirement = repository.get_requirement_for_key_share(
            schedule_id, requirement_id
        )
        if requirement is None:
            raise AssignmentRequirementNotFound

        target = repository.get_user_for_share(user_id)
        if target is None:
            raise AssignmentUserNotFound
        user, role_code = target
        if role_code != "employee" or not user.is_active or user.activated_at is None:
            raise AssignmentUserIneligible

        if (
            repository.count_for_requirement(requirement_id)
            >= requirement.required_workers
        ):
            raise AssignmentCapacityExceeded
        if repository.has_duplicate(requirement_id, user_id):
            raise AssignmentAlreadyExists
        if repository.has_overlap(user_id, requirement):
            raise AssignmentOverlap

        entries = repository.submitted_entries(
            schedule_id, user_id, requirement.work_date
        )
        availability = _availability_status(requirement, entries)
        if availability != "available" and not warning_acknowledged:
            raise AssignmentWarningRequired

        now = datetime.now(UTC)
        assignment = ShiftAssignment(
            shift_requirement_id=requirement.id,
            user_id=user_id,
            assigned_by=actor_id,
            assigned_at=now,
            warning_type=None if availability == "available" else availability,
            warning_acknowledged_at=None if availability == "available" else now,
        )
        repository.add(assignment)
        session.flush()
        response = assignment_response(assignment, requirement)
        session.commit()
        return response
    except DBAPIError as exc:
        session.rollback()
        conflict = _assignment_db_conflict(exc)
        if conflict is not None:
            raise conflict from None
        raise
    except Exception:
        session.rollback()
        raise
