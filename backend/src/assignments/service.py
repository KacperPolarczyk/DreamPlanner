"""Application-level authorization and existence checks for assignment reads."""

from datetime import date

from sqlalchemy.orm import Session

from src.assignments.models import ShiftAssignment
from src.assignments.repository import ShiftAssignmentRepository
from src.schedules.models import ShiftRequirement
from src.schedules.repository import ScheduleRepository


class AssignmentReadForbidden(Exception):
    """The caller's current role does not grant manager assignment reads."""


class AssignmentScheduleNotFound(Exception):
    """The requested schedule does not exist."""


class AssignmentNotFound(Exception):
    """The assignment does not belong to the requested schedule."""


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
