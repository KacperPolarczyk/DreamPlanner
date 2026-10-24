"""Availability submission workflows and visibility rules."""

import sqlite3
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.availability.models import AvailabilityEntry, AvailabilitySubmission
from src.availability.repository import AvailabilitySubmissionRepository
from src.availability.schemas import (
    AvailabilityEntryInput,
    AvailabilityEntryResponse,
    AvailabilitySubmissionResponse,
)
from src.schedules.repository import ScheduleRepository
from src.schedules.service import _validate_requirement_period


class AvailabilityScheduleNotFound(Exception):
    """The requested schedule does not exist."""


class AvailabilitySubmissionNotFound(Exception):
    """The requested submission is absent or outside the caller's scope."""


class AvailabilityScheduleStateConflict(Exception):
    """The schedule is not accepting availability declarations."""


class AvailabilitySubmissionConflict(Exception):
    """The employee already has a declaration for this schedule."""


def _submission_conflict(exc: IntegrityError) -> bool:
    """Match only the PostgreSQL declaration uniqueness constraint."""
    original = exc.orig
    diagnostic = getattr(original, "diag", None)
    return (
        getattr(original, "sqlstate", None) == "23505"
        and getattr(diagnostic, "constraint_name", None) == "uq_availability_submission"
    ) or (
        isinstance(original, sqlite3.IntegrityError)
        and "availability_submissions.schedule_id, availability_submissions.user_id"
        in str(original)
    )


def create_availability_submission(
    session: Session,
    schedule_id: int,
    user_id: int,
    *,
    entries: list[AvailabilityEntryInput],
    submit: bool,
) -> AvailabilitySubmissionResponse:
    """Validate, insert, and commit one employee declaration atomically."""
    repository = AvailabilitySubmissionRepository(session)
    try:
        schedule = repository.get_schedule_for_update(schedule_id)
        if schedule is None:
            raise AvailabilityScheduleNotFound
        if schedule.status != "collecting":
            raise AvailabilityScheduleStateConflict
        if repository.get_for_user(schedule_id, user_id) is not None:
            raise AvailabilitySubmissionConflict

        ordered = sorted(entries, key=lambda entry: (entry.work_date, entry.start_time))
        previous_by_day: dict[object, object] = {}
        for entry in ordered:
            _validate_requirement_period(
                schedule, entry.work_date, entry.start_time, entry.end_time
            )
            previous_end = previous_by_day.get(entry.work_date)
            if previous_end is not None and entry.start_time < previous_end:
                raise ValueError("availability entries overlap")
            previous_by_day[entry.work_date] = entry.end_time

        now = datetime.now(UTC)
        submission = AvailabilitySubmission(
            schedule_id=schedule_id,
            user_id=user_id,
            submitted_at=now if submit else None,
            created_at=now,
            updated_at=now,
        )
        repository.add_submission(submission)
        session.flush()
        persisted_entries = [
            AvailabilityEntry(
                submission_id=submission.id,
                work_date=entry.work_date,
                start_time=entry.start_time,
                end_time=entry.end_time,
                status=entry.status,
                note=entry.note,
            )
            for entry in entries
        ]
        repository.add_entries(persisted_entries)
        session.flush()
        response = AvailabilitySubmissionResponse(
            id=str(submission.id),
            schedule_id=str(schedule_id),
            user_id=str(user_id),
            submitted_at=submission.submitted_at,
            created_at=now,
            updated_at=now,
            entries=[
                AvailabilityEntryResponse(
                    id=str(entry.id),
                    work_date=entry.work_date,
                    start_time=entry.start_time,
                    end_time=entry.end_time,
                    status=entry.status,
                    note=entry.note,
                )
                for entry in sorted(
                    persisted_entries,
                    key=lambda item: (item.work_date, item.start_time, item.id),
                )
            ],
        )
        session.commit()
        return response
    except IntegrityError as exc:
        session.rollback()
        if _submission_conflict(exc):
            raise AvailabilitySubmissionConflict from None
        raise
    except Exception:
        session.rollback()
        raise


def list_availability_submissions(
    session: Session,
    schedule_id: int,
    *,
    role: Literal["employee", "manager", "admin"],
    authenticated_user_id: int,
    user_id: int | None,
    state: Literal["draft", "submitted"] | None,
    limit: int,
    offset: int,
) -> tuple[list[AvailabilitySubmission], int]:
    """Return a schedule page restricted to the caller's role and identity."""
    schedule = ScheduleRepository(session).get_by_id(schedule_id)
    if schedule is None:
        raise AvailabilityScheduleNotFound

    visible_user_id = authenticated_user_id if role == "employee" else None
    return AvailabilitySubmissionRepository(session).list_for_schedule(
        schedule_id,
        visible_user_id=visible_user_id,
        user_id=user_id,
        state=state,
        limit=limit,
        offset=offset,
    )


def read_availability_submission(
    session: Session,
    schedule_id: int,
    submission_id: int,
    *,
    role: Literal["employee", "manager", "admin"],
    authenticated_user_id: int,
) -> tuple[AvailabilitySubmission, list[AvailabilityEntry]]:
    """Return one visible submission and its sorted entries."""
    if ScheduleRepository(session).get_by_id(schedule_id) is None:
        raise AvailabilityScheduleNotFound

    visible_user_id = authenticated_user_id if role == "employee" else None
    repository = AvailabilitySubmissionRepository(session)
    submission = repository.get_for_schedule(
        schedule_id, submission_id, visible_user_id=visible_user_id
    )
    if submission is None:
        raise AvailabilitySubmissionNotFound
    return submission, repository.list_entries(submission.id)
