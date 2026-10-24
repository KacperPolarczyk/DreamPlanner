"""Read workflows and visibility rules for availability submissions."""

from typing import Literal

from sqlalchemy.orm import Session

from src.availability.models import AvailabilityEntry, AvailabilitySubmission
from src.availability.repository import AvailabilitySubmissionRepository
from src.schedules.repository import ScheduleRepository


class AvailabilityScheduleNotFound(Exception):
    """The requested schedule does not exist."""


class AvailabilitySubmissionNotFound(Exception):
    """The requested submission is absent or outside the caller's scope."""


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
