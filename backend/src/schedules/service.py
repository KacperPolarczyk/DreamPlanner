"""Monthly schedule writing workflows and their expected conflicts."""

import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.positions.models import Position
from src.schedules.models import Schedule, ShiftRequirement
from src.schedules.repository import ScheduleRepository, ShiftRequirementRepository
from src.users.repository import UserRepository


class SchedulePeriodConflict(Exception):
    """A schedule already exists for the requested year and month."""


class ScheduleNotFound(Exception):
    """The requested schedule does not exist."""


class ScheduleStateConflict(Exception):
    """The current schedule status forbids the requested operation."""


class SchedulePublicationForbidden(Exception):
    """The caller is no longer an active manager."""


class SchedulePublicationBlocked(Exception):
    """A business rule prevents publishing the schedule."""

    def __init__(
        self, reason: Literal["no_requirements", "overstaffed", "ineligible_user"]
    ):
        self.reason = reason
        super().__init__(reason)


class PositionNotFound(Exception):
    """The requested position does not exist."""


class PositionInactive(Exception):
    """The requested position is inactive."""


class ShiftRequirementConflict(Exception):
    """The exact staffing requirement already exists."""


class ShiftRequirementAssigned(Exception):
    """A structural change is forbidden while assignments exist."""


class ShiftRequirementNotFound(Exception):
    """The requirement does not belong to the requested schedule."""


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


def _validate_requirement_period(
    schedule: Schedule, work_date: date, start_time: time, end_time: time
) -> None:
    """Validate the local date and time range against its schedule and DST."""
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


def create_shift_requirement(
    session: Session,
    schedule_id: int,
    *,
    position_id: int,
    work_date: date,
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
        _validate_requirement_period(schedule, work_date, start_time, end_time)
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
        session.expunge(requirement)
        session.expunge(position)
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


def update_shift_requirement(
    session: Session,
    schedule_id: int,
    requirement_id: int,
    *,
    changes: dict[str, object],
) -> tuple[ShiftRequirement, Position]:
    """Apply a sparse PATCH while protecting assigned shifts from restructuring."""
    try:
        schedule = ScheduleRepository(session).get_by_id_for_update(schedule_id)
        if schedule is None:
            raise ScheduleNotFound
        if schedule.status != "draft":
            raise ScheduleStateConflict

        repository = ShiftRequirementRepository(session)
        requirement = repository.get_for_schedule_for_update(
            schedule_id, requirement_id
        )
        if requirement is None:
            raise ShiftRequirementNotFound

        old = {
            "position_id": requirement.position_id,
            "work_date": requirement.work_date,
            "start_time": requirement.start_time,
            "end_time": requirement.end_time,
            "required_workers": requirement.required_workers,
        }
        new = old | changes
        if "position_id" in new:
            new["position_id"] = int(new["position_id"])
        structural_changes = {key for key in old if old[key] != new[key]}
        _validate_requirement_period(
            schedule,
            new["work_date"],
            new["start_time"],
            new["end_time"],
        )
        if new["required_workers"] <= 0:
            raise ValueError("required_workers must be positive")

        if "position_id" in structural_changes:
            position = repository.get_position_for_share(new["position_id"])
            if position is None:
                raise PositionNotFound
            if not position.is_active:
                raise PositionInactive

        if structural_changes and repository.has_assignments(requirement_id):
            raise ShiftRequirementAssigned

        for key, value in new.items():
            setattr(requirement, key, value)
        if "note" in changes:
            requirement.note = changes["note"]
        session.flush()
        result = repository.get_committed(requirement_id)
        session.expunge(result[0])
        session.expunge(result[1])
        session.commit()
        return result
    except IntegrityError as exc:
        session.rollback()
        if _requirement_conflict(exc):
            raise ShiftRequirementConflict from None
        raise
    except Exception:
        session.rollback()
        raise


def delete_shift_requirement(
    session: Session, schedule_id: int, requirement_id: int
) -> None:
    """Delete an unassigned requirement from a locked draft schedule."""
    try:
        schedules = ScheduleRepository(session)
        schedule = schedules.get_by_id_for_update(schedule_id)
        if schedule is None:
            raise ScheduleNotFound
        if schedule.status != "draft":
            raise ScheduleStateConflict

        requirements = ShiftRequirementRepository(session)
        requirement = requirements.get_for_schedule_for_update(
            schedule_id, requirement_id
        )
        if requirement is None:
            raise ShiftRequirementNotFound
        if requirements.has_assignments(requirement_id):
            raise ShiftRequirementAssigned

        requirements.delete(requirement)
        session.flush()
        session.commit()
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


def delete_schedule(session: Session, schedule_id: int) -> None:
    """Delete a locked schedule and its FK-cascaded history atomically.

    The parent lock serializes concurrent stage changes and child inserts.
    PostgreSQL cascades requirements, submissions, entries, and assignments
    within the same transaction. A failed flush or commit is rolled back.
    """
    try:
        repository = ScheduleRepository(session)
        schedule = repository.get_by_id_for_update(schedule_id)
        if schedule is None:
            raise ScheduleNotFound
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


def publish_schedule(session: Session, schedule_id: int, *, actor_id: int) -> Schedule:
    """Validate and publish one closed schedule atomically.

    The schedule lock serializes API assignment writes. Requirement locks and
    all relevant account locks are acquired in deterministic order. Account
    locks include the actor so a concurrent role change cannot authorize a
    stale manager identity.
    """
    try:
        schedules = ScheduleRepository(session)
        schedule = schedules.get_by_id_for_update(schedule_id)
        if schedule is None:
            raise ScheduleNotFound
        if schedule.status != "closed":
            raise ScheduleStateConflict

        requirements = schedules.lock_requirements_for_publication(schedule_id)
        assigned_user_ids = schedules.assignment_user_ids(schedule_id)
        user_ids = assigned_user_ids | {actor_id}
        locked_users = UserRepository(session).lock_users_for_share(user_ids)
        users_by_id = {user.id: (user, role_code) for user, role_code in locked_users}

        actor = users_by_id.get(actor_id)
        if (
            actor is None
            or actor[1] != "manager"
            or not actor[0].is_active
            or actor[0].activated_at is None
        ):
            raise SchedulePublicationForbidden

        if not requirements:
            raise SchedulePublicationBlocked("no_requirements")

        counts = schedules.assignment_counts_by_requirement(schedule_id)
        if any(
            counts[requirement.id] > requirement.required_workers
            for requirement in requirements
        ):
            raise SchedulePublicationBlocked("overstaffed")

        if any(
            user_id not in users_by_id
            or users_by_id[user_id][1] != "employee"
            or not users_by_id[user_id][0].is_active
            or users_by_id[user_id][0].activated_at is None
            for user_id in assigned_user_ids
        ):
            raise SchedulePublicationBlocked("ineligible_user")

        schedule.status = "published"
        schedule.published_at = datetime.now(UTC)
        session.flush()
        session.commit()
        return schedule
    except Exception:
        session.rollback()
        raise
