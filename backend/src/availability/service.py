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
    UpdateAvailabilitySubmissionInput,
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


class AvailabilityEntryConflict(Exception):
    """A stored availability entry conflicts with an existing database row."""


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


def _entry_conflict(exc: IntegrityError) -> bool:
    """Recognize only known availability-entry uniqueness and overlap errors."""
    original = exc.orig
    diagnostic = getattr(original, "diag", None)
    sqlstate = getattr(original, "sqlstate", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    message = getattr(diagnostic, "message_primary", None)
    if sqlstate == "23505" and constraint_name == "uq_availability_entry":
        return True
    if sqlstate == "P0001" and message == "Nakladajace sie przedzialy dyspozycyjnosci":
        return True
    return isinstance(original, sqlite3.IntegrityError) and (
        "availability_entries.submission_id, availability_entries.work_date, "
        "availability_entries.start_time, availability_entries.end_time"
        in str(original)
    )


def _validate_entries(schedule, entries: list[AvailabilityEntryInput]) -> None:
    """Validate each entry and reject overlap in the complete replacement list."""
    ordered = sorted(entries, key=lambda entry: (entry.work_date, entry.start_time))
    previous_by_day = {}
    for entry in ordered:
        _validate_requirement_period(
            schedule, entry.work_date, entry.start_time, entry.end_time
        )
        previous_end = previous_by_day.get(entry.work_date)
        if previous_end is not None and entry.start_time < previous_end:
            raise ValueError("availability entries overlap")
        previous_by_day[entry.work_date] = entry.end_time


def _utc_timestamp(value: datetime | None) -> datetime | None:
    """Normalize database timestamps for the public UTC representation."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _submission_response(
    submission: AvailabilitySubmission, entries: list[AvailabilityEntry]
) -> AvailabilitySubmissionResponse:
    """Build a fully materialized response independent of session loading."""
    return AvailabilitySubmissionResponse(
        id=str(submission.id),
        schedule_id=str(submission.schedule_id),
        user_id=str(submission.user_id),
        submitted_at=_utc_timestamp(submission.submitted_at),
        created_at=_utc_timestamp(submission.created_at),
        updated_at=_utc_timestamp(submission.updated_at),
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
                entries, key=lambda item: (item.work_date, item.start_time, item.id)
            )
        ],
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

        _validate_entries(schedule, entries)

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
        response = _submission_response(submission, persisted_entries)
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


def update_availability_submission(
    session: Session,
    schedule_id: int,
    submission_id: int,
    user_id: int,
    *,
    changes: UpdateAvailabilitySubmissionInput,
) -> AvailabilitySubmissionResponse:
    """Apply one full-list replacement or submission update atomically."""
    repository = AvailabilitySubmissionRepository(session)
    entries_provided = "entries" in changes.model_fields_set
    try:
        schedule = repository.get_schedule_for_update(schedule_id)
        if schedule is None:
            raise AvailabilityScheduleNotFound
        if schedule.status != "collecting":
            raise AvailabilityScheduleStateConflict
        submission = repository.get_for_user_for_update(
            schedule_id, submission_id, user_id
        )
        if submission is None:
            raise AvailabilitySubmissionNotFound

        now = datetime.now(UTC)
        if entries_provided:
            _validate_entries(schedule, changes.entries)
            repository.delete_entries(submission.id)
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
                for entry in changes.entries
            ]
            repository.add_entries(persisted_entries)
            submission.submitted_at = now if changes.submit else None
        else:
            persisted_entries = repository.list_entries(submission.id)
            submission.submitted_at = now

        submission.updated_at = now
        session.flush()
        if entries_provided:
            response_entries = persisted_entries
        else:
            response_entries = repository.list_entries(submission.id)
        response = _submission_response(submission, response_entries)
        session.commit()
        return response
    except IntegrityError as exc:
        session.rollback()
        if _entry_conflict(exc):
            raise AvailabilityEntryConflict from None
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
