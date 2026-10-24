"""Schedule-scoped SQLAlchemy reads for shift assignments."""

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.assignments.models import ShiftAssignment
from src.availability.models import AvailabilityEntry, AvailabilitySubmission
from src.schedules.models import ShiftRequirement
from src.users.models import Role, User


class ShiftAssignmentRepository:
    """Read assignments and shift intervals in bounded SQL queries."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_requirement_for_key_share(
        self, schedule_id: int, requirement_id: int
    ) -> ShiftRequirement | None:
        """Lock the scoped requirement before reading its structural fields."""
        return self.session.scalar(
            select(ShiftRequirement)
            .where(
                ShiftRequirement.schedule_id == schedule_id,
                ShiftRequirement.id == requirement_id,
            )
            .with_for_update(read=True, key_share=True, of=ShiftRequirement)
            .execution_options(populate_existing=True)
        )

    def get_user_for_share(self, user_id: int) -> tuple[User, str] | None:
        """Lock target account against role and activation changes."""
        return self.session.execute(
            select(User, Role.code)
            .join(Role, Role.id == User.role_id)
            .where(User.id == user_id)
            .with_for_update(read=True, of=User)
            .execution_options(populate_existing=True)
        ).one_or_none()

    def count_for_requirement(self, requirement_id: int) -> int:
        """Count committed occupied places after the schedule lock."""
        return (
            self.session.scalar(
                select(func.count(ShiftAssignment.id)).where(
                    ShiftAssignment.shift_requirement_id == requirement_id
                )
            )
            or 0
        )

    def has_duplicate(self, requirement_id: int, user_id: int) -> bool:
        """Check the exact assignment pair after the schedule lock."""
        return (
            self.session.scalar(
                select(ShiftAssignment.id)
                .where(
                    ShiftAssignment.shift_requirement_id == requirement_id,
                    ShiftAssignment.user_id == user_id,
                )
                .limit(1)
            )
            is not None
        )

    def has_overlap(self, user_id: int, requirement: ShiftRequirement) -> bool:
        """Find any overlapping assignment, including another schedule."""
        return (
            self.session.scalar(
                select(ShiftAssignment.id)
                .join(ShiftRequirement)
                .where(
                    ShiftAssignment.user_id == user_id,
                    ShiftRequirement.work_date == requirement.work_date,
                    ShiftRequirement.start_time < requirement.end_time,
                    requirement.start_time < ShiftRequirement.end_time,
                )
                .limit(1)
            )
            is not None
        )

    def submitted_entries(
        self, schedule_id: int, user_id: int, work_date: date
    ) -> list[AvailabilityEntry]:
        """Read intervals only from the user's submitted monthly declaration."""
        return list(
            self.session.scalars(
                select(AvailabilityEntry)
                .join(AvailabilitySubmission)
                .where(
                    AvailabilitySubmission.schedule_id == schedule_id,
                    AvailabilitySubmission.user_id == user_id,
                    AvailabilitySubmission.submitted_at.is_not(None),
                    AvailabilityEntry.work_date == work_date,
                )
                .order_by(
                    AvailabilityEntry.start_time.asc(), AvailabilityEntry.id.asc()
                )
            ).all()
        )

    def add(self, assignment: ShiftAssignment) -> None:
        """Stage the assignment without committing the caller's transaction."""
        self.session.add(assignment)

    def list_for_schedule(
        self,
        schedule_id: int,
        *,
        work_date: date | None,
        shift_requirement_id: int | None,
        user_id: int | None,
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[ShiftAssignment, ShiftRequirement]], int]:
        """Filter within one schedule before counting and paginating."""
        filters = [ShiftRequirement.schedule_id == schedule_id]
        if work_date is not None:
            filters.append(ShiftRequirement.work_date == work_date)
        if shift_requirement_id is not None:
            filters.append(ShiftAssignment.shift_requirement_id == shift_requirement_id)
        if user_id is not None:
            filters.append(ShiftAssignment.user_id == user_id)

        total = self.session.scalar(
            select(func.count())
            .select_from(ShiftAssignment)
            .join(ShiftRequirement)
            .where(*filters)
        )
        rows = self.session.execute(
            select(ShiftAssignment, ShiftRequirement)
            .join(ShiftRequirement)
            .where(*filters)
            .order_by(
                ShiftRequirement.work_date.asc(),
                ShiftRequirement.start_time.asc(),
                ShiftAssignment.shift_requirement_id.asc(),
                ShiftAssignment.id.asc(),
            )
            .limit(limit)
            .offset(offset)
        ).all()
        return list(rows), total or 0

    def get_for_schedule(
        self, schedule_id: int, assignment_id: int
    ) -> tuple[ShiftAssignment, ShiftRequirement] | None:
        """Look up an assignment only through its schedule's requirement."""
        return self.session.execute(
            select(ShiftAssignment, ShiftRequirement)
            .join(ShiftRequirement)
            .where(
                ShiftRequirement.schedule_id == schedule_id,
                ShiftAssignment.id == assignment_id,
            )
        ).one_or_none()
