"""Schedule-scoped SQLAlchemy reads for shift assignments."""

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.assignments.models import ShiftAssignment
from src.schedules.models import ShiftRequirement


class ShiftAssignmentRepository:
    """Read assignments and shift intervals in bounded SQL queries."""

    def __init__(self, session: Session) -> None:
        self.session = session

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
