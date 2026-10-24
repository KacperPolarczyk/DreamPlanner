"""Monthly schedule writing workflows and their expected conflicts."""

import sqlite3
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.schedules.models import Schedule
from src.schedules.repository import ScheduleRepository


class SchedulePeriodConflict(Exception):
    """A schedule already exists for the requested year and month."""


class ScheduleNotFound(Exception):
    """The requested schedule does not exist."""


class ScheduleStateConflict(Exception):
    """The current schedule status forbids the requested operation."""


class ScheduleNotEmpty(Exception):
    """The schedule has direct or indirect dependent data."""


def _period_conflict(exc: IntegrityError) -> bool:
    """Recognize only the schedules year and month unique constraint."""
    original = exc.orig
    diagnostic = getattr(original, "diag", None)
    if getattr(original, "sqlstate", None) == "23505":
        return getattr(diagnostic, "constraint_name", None) == "uq_schedules_period"
    return isinstance(original, sqlite3.IntegrityError) and str(original) == (
        "UNIQUE constraint failed: schedules.year, schedules.month"
    )


def create_schedule(
    session: Session, year: int, month: int, creator_id: int
) -> Schedule:
    """Persist an empty draft, returning it only after a successful commit.

    Raises:
        SchedulePeriodConflict: If the requested period is already occupied.
        IntegrityError: If another database constraint fails.
    """
    try:
        schedule = Schedule(year=year, month=month, created_by=creator_id)
        ScheduleRepository(session).add(schedule)
        session.flush()
        session.commit()
        return schedule
    except IntegrityError as exc:
        session.rollback()
        if _period_conflict(exc):
            raise SchedulePeriodConflict("Schedule period already exists.") from None
        raise
    except Exception:
        session.rollback()
        raise


def delete_empty_draft(session: Session, schedule_id: int) -> None:
    """Delete a draft only after locking it and checking both direct child tables.

    PostgreSQL's immediate child FKs take KEY SHARE on the parent, which
    conflicts with FOR UPDATE. Under READ COMMITTED, child checks issued after
    the lock see a competing insert that committed while this call waited.
    Future child writers must keep their FK checks in the same transaction.

    Raises:
        ScheduleNotFound: If the row does not exist when the lock is acquired.
        ScheduleStateConflict: If its current status is not draft.
        ScheduleNotEmpty: If either direct child table contains a row.
    """
    try:
        repository = ScheduleRepository(session)
        schedule = repository.get_by_id_for_update(schedule_id)
        if schedule is None:
            raise ScheduleNotFound
        if schedule.status != "draft":
            raise ScheduleStateConflict
        if repository.has_shift_requirements(schedule_id):
            raise ScheduleNotEmpty
        if repository.has_availability_submissions(schedule_id):
            raise ScheduleNotEmpty
        repository.delete(schedule)
        session.flush()
        session.commit()
    except Exception:
        session.rollback()
        raise


def open_collection(session: Session, schedule_id: int) -> Schedule:
    """Move one locked draft to collecting and commit its first opening time.

    Raises:
        ScheduleNotFound: If the schedule no longer exists.
        ScheduleStateConflict: If its current status is not draft.
    """
    try:
        schedule = ScheduleRepository(session).get_by_id_for_update(schedule_id)
        if schedule is None:
            raise ScheduleNotFound
        if schedule.status != "draft":
            raise ScheduleStateConflict
        schedule.status = "collecting"
        schedule.collection_opened_at = datetime.now(UTC)
        session.flush()
        session.commit()
        return schedule
    except Exception:
        session.rollback()
        raise
