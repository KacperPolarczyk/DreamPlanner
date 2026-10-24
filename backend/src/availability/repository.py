"""Read queries for monthly availability submissions."""

from typing import Literal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from src.availability.models import AvailabilityEntry, AvailabilitySubmission
from src.schedules.models import Schedule


class AvailabilitySubmissionRepository:
    """Access availability records with caller supplied visibility filters."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_schedule_for_update(self, schedule_id: int) -> Schedule | None:
        """Lock and refresh the schedule before checking its write state."""
        return self.session.scalar(
            select(Schedule)
            .where(Schedule.id == schedule_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def get_for_user(
        self, schedule_id: int, user_id: int
    ) -> AvailabilitySubmission | None:
        """Return the existing declaration for one employee and schedule."""
        return self.session.scalar(
            select(AvailabilitySubmission).where(
                AvailabilitySubmission.schedule_id == schedule_id,
                AvailabilitySubmission.user_id == user_id,
            )
        )

    def get_for_user_for_update(
        self, schedule_id: int, submission_id: int, user_id: int
    ) -> AvailabilitySubmission | None:
        """Lock one submission constrained by schedule and employee ownership."""
        return self.session.scalar(
            select(AvailabilitySubmission)
            .where(
                AvailabilitySubmission.schedule_id == schedule_id,
                AvailabilitySubmission.id == submission_id,
                AvailabilitySubmission.user_id == user_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def delete_entries(self, submission_id: int) -> None:
        """Delete all old entries immediately before replacement inserts."""
        self.session.execute(
            delete(AvailabilityEntry)
            .where(AvailabilityEntry.submission_id == submission_id)
            .execution_options(synchronize_session=False)
        )

    def add_submission(self, submission: AvailabilitySubmission) -> None:
        """Stage a declaration in the caller-owned transaction."""
        self.session.add(submission)

    def add_entries(self, entries: list[AvailabilityEntry]) -> None:
        """Stage declaration entries in the caller-owned transaction."""
        self.session.add_all(entries)

    def get_entries(self, submission_id: int) -> list[AvailabilityEntry]:
        """Return entries in the public response order."""
        return self.list_entries(submission_id)

    def list_for_schedule(
        self,
        schedule_id: int,
        *,
        visible_user_id: int | None,
        user_id: int | None,
        state: Literal["draft", "submitted"] | None,
        limit: int,
        offset: int,
    ) -> tuple[list[AvailabilitySubmission], int]:
        """Return one filtered page; visible_user_id always scopes employee data."""
        filters = [AvailabilitySubmission.schedule_id == schedule_id]
        if visible_user_id is not None:
            filters.append(AvailabilitySubmission.user_id == visible_user_id)
        elif user_id is not None:
            filters.append(AvailabilitySubmission.user_id == user_id)
        if state == "draft":
            filters.append(AvailabilitySubmission.submitted_at.is_(None))
        elif state == "submitted":
            filters.append(AvailabilitySubmission.submitted_at.is_not(None))

        total = self.session.scalar(
            select(func.count()).select_from(AvailabilitySubmission).where(*filters)
        )
        submissions = self.session.scalars(
            select(AvailabilitySubmission)
            .where(*filters)
            .order_by(
                AvailabilitySubmission.user_id.asc(),
                AvailabilitySubmission.id.asc(),
            )
            .limit(limit)
            .offset(offset)
        ).all()
        return list(submissions), total or 0

    def get_for_schedule(
        self,
        schedule_id: int,
        submission_id: int,
        *,
        visible_user_id: int | None,
    ) -> AvailabilitySubmission | None:
        """Return one submission constrained by schedule and optional owner."""
        filters = [
            AvailabilitySubmission.schedule_id == schedule_id,
            AvailabilitySubmission.id == submission_id,
        ]
        if visible_user_id is not None:
            filters.append(AvailabilitySubmission.user_id == visible_user_id)
        return self.session.scalar(select(AvailabilitySubmission).where(*filters))

    def list_entries(self, submission_id: int) -> list[AvailabilityEntry]:
        """Return a submission's intervals in deterministic local-time order."""
        return list(
            self.session.scalars(
                select(AvailabilityEntry)
                .where(AvailabilityEntry.submission_id == submission_id)
                .order_by(
                    AvailabilityEntry.work_date.asc(),
                    AvailabilityEntry.start_time.asc(),
                    AvailabilityEntry.id.asc(),
                )
            ).all()
        )
