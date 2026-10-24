"""Monthly schedule writing workflows and their expected conflicts."""

import sqlite3
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.positions.models import Position
from src.schedules.models import Schedule, ShiftRequirement
from src.schedules.repository import ScheduleRepository, ShiftRequirementRepository


class SchedulePeriodConflict(Exception):
    """A schedule already exists for the requested year and month."""


class ScheduleNotFound(Exception):
    """The requested schedule does not exist."""


class ScheduleStateConflict(Exception):
    """The current schedule status forbids the requested operation."""


class ScheduleNotEmpty(Exception):
    """The schedule has direct or indirect dependent data."""


class PositionNotFound(Exception):
    """The requested position does not exist."""


class PositionInactive(Exception):
    """The requested position is inactive."""


class ShiftRequirementConflict(Exception):
    """The exact staffing requirement already exists."""


def _requirement_conflict(exc: IntegrityError) -> bool:
    original = exc.orig
    diagnostic = getattr(original, "diag", None)
    return (
        getattr(original, "sqlstate", None) == "23505"
        and getattr(diagnostic, "constraint_name", None) == "uq_shift_requirement"
    )


def _valid_local_time(day, value: time) -> bool:
    zone = ZoneInfo("Europe/Warsaw")
    naive = datetime.combine(day, value)
    valid_offsets = {
        localized.utcoffset()
        for fold in (0, 1)
        if (localized := naive.replace(tzinfo=zone, fold=fold))
        .astimezone(UTC)
        .astimezone(zone)
        .replace(tzinfo=None)
        == naive
    }
    return len(valid_offsets) == 1


def create_shift_requirement(
    session: Session,
    schedule_id: int,
    *,
    position_id: int,
    work_date,
    start_time: time,
    end_time: time,
    required_workers: int,
    note: str | None,
) -> tuple[ShiftRequirement, Position]:
    """Validate, insert, and commit a staffing requirement atomically."""
    try:
        schedule = ScheduleRepository(session).get_by_id_for_update(schedule_id)
        if schedule is None:
            raise ScheduleNotFound
        if schedule.status != "draft":
            raise ScheduleStateConflict
        requirements = ShiftRequirementRepository(session)
        position = requirements.get_position_for_share(position_id)
        if position is None:
            raise PositionNotFound
        if not position.is_active:
            raise PositionInactive
        if work_date.year != schedule.year or work_date.month != schedule.month:
            raise ValueError("work_date is outside schedule period")
        if start_time >= end_time:
            raise ValueError("start_time must be before end_time")
        cursor = datetime.combine(work_date, start_time)
        finish = datetime.combine(work_date, end_time)
        while cursor < finish:
            if not _valid_local_time(work_date, cursor.time()):
                raise ValueError("interval includes invalid or ambiguous local time")
            cursor += timedelta(minutes=1)
        if not _valid_local_time(work_date, end_time):
            raise ValueError("interval ends at invalid or ambiguous local time")
        requirement = ShiftRequirement(
            schedule_id=schedule_id,
            position_id=position_id,
            work_date=work_date,
            start_time=start_time,
            end_time=end_time,
            required_workers=required_workers,
            note=note,
        )
        requirements.add(requirement)
        session.flush()
        requirement, position = requirements.get_committed(requirement.id)
        session.commit()
        return requirement, position
    except IntegrityError as exc:
        session.rollback()
        if _requirement_conflict(exc):
            raise ShiftRequirementConflict from None
        raise
    except Exception:
        session.rollback()
        raise


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


def _change_schedule_stage(
    session: Session,
    schedule_id: int,
    *,
    source_status: Literal["draft", "collecting", "collection_closed"],
    target_status: Literal["collecting", "collection_closed", "closed"],
    timestamp_field: Literal["collection_opened_at", "collection_closed_at"] | None,
) -> Schedule:
    """Lock, validate, and commit one stage and its optional UTC timestamp."""
    try:
        schedule = ScheduleRepository(session).get_by_id_for_update(schedule_id)
        if schedule is None:
            raise ScheduleNotFound
        if schedule.status != source_status:
            raise ScheduleStateConflict
        schedule.status = target_status
        if timestamp_field is not None:
            setattr(schedule, timestamp_field, datetime.now(UTC))
        session.flush()
        session.commit()
        return schedule
    except Exception:
        session.rollback()
        raise


def open_collection(session: Session, schedule_id: int) -> Schedule:
    """Move one draft to collecting and commit its first opening time.

    Raises:
        ScheduleNotFound: If the schedule no longer exists.
        ScheduleStateConflict: If its current status is not draft.
    """
    return _change_schedule_stage(
        session,
        schedule_id,
        source_status="draft",
        target_status="collecting",
        timestamp_field="collection_opened_at",
    )


def close_collection(session: Session, schedule_id: int) -> Schedule:
    """Move one collecting schedule to collection_closed and commit its time.

    Raises:
        ScheduleNotFound: If the schedule no longer exists.
        ScheduleStateConflict: If its current status is not collecting.
    """
    return _change_schedule_stage(
        session,
        schedule_id,
        source_status="collecting",
        target_status="collection_closed",
        timestamp_field="collection_closed_at",
    )


def close_schedule(session: Session, schedule_id: int) -> Schedule:
    """Move one collection_closed schedule to closed without changing timestamps.

    Raises:
        ScheduleNotFound: If the schedule no longer exists.
        ScheduleStateConflict: If its current status is not collection_closed.
    """
    return _change_schedule_stage(
        session,
        schedule_id,
        source_status="collection_closed",
        target_status="closed",
        timestamp_field=None,
    )
