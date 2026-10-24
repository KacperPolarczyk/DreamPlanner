"""Persistence and read queries for monthly schedules."""

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.assignments.models import ShiftAssignment
from src.availability.models import AvailabilitySubmission
from src.positions.models import Position
from src.schedules.models import Schedule, ShiftRequirement


class ScheduleRepository:
    """Access schedules with one request-scoped database session."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_id(self, schedule_id: int) -> Schedule | None:
        """Return a schedule regardless of status or creator."""
        return self.session.get(Schedule, schedule_id)

    def get_by_id_for_update(self, schedule_id: int) -> Schedule | None:
        """Lock one schedule and refresh any identity-map copy of it."""
        return self.session.scalar(
            select(Schedule)
            .where(Schedule.id == schedule_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def has_shift_requirements(self, schedule_id: int) -> bool:
        """Check for direct required-shift children after locking the schedule."""
        return (
            self.session.scalar(
                select(ShiftRequirement.id)
                .where(ShiftRequirement.schedule_id == schedule_id)
                .limit(1)
            )
            is not None
        )

    def has_availability_submissions(self, schedule_id: int) -> bool:
        """Check for direct availability children after locking the schedule."""
        return (
            self.session.scalar(
                select(AvailabilitySubmission.id)
                .where(AvailabilitySubmission.schedule_id == schedule_id)
                .limit(1)
            )
            is not None
        )

    def delete(self, schedule: Schedule) -> None:
        """Stage deletion in the caller's transaction."""
        self.session.delete(schedule)

    def add(self, schedule: Schedule) -> None:
        """Stage a new schedule without committing the transaction."""
        self.session.add(schedule)

    def list_schedules(
        self,
        *,
        year: int | None,
        month: int | None,
        status: str | None,
        limit: int,
        offset: int,
        sort: str | None,
    ) -> tuple[list[Schedule], int]:
        """Return a deterministic filtered page and its unpaginated count."""
        filters = []
        if year is not None:
            filters.append(Schedule.year == year)
        if month is not None:
            filters.append(Schedule.month == month)
        if status is not None:
            filters.append(Schedule.status == status)

        total = self.session.scalar(
            select(func.count()).select_from(Schedule).where(*filters)
        )
        if sort is None:
            ordering = (Schedule.year.desc(), Schedule.month.desc())
            id_order = Schedule.id.desc()
        else:
            primary = (
                Schedule.year if sort.removeprefix("-") == "year" else Schedule.month
            )
            secondary = Schedule.month if primary is Schedule.year else Schedule.year
            direction = "desc" if sort.startswith("-") else "asc"
            ordering = (getattr(primary, direction)(), getattr(secondary, direction)())
            id_order = Schedule.id.asc()

        schedules = self.session.scalars(
            select(Schedule)
            .where(*filters)
            .order_by(*ordering, id_order)
            .limit(limit)
            .offset(offset)
        ).all()
        return list(schedules), total or 0


class ShiftRequirementRepository:
    """Read staffing requirements with their position details."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_position_for_share(self, position_id: int) -> Position | None:
        """Lock a position against concurrent deactivation until commit."""
        return self.session.scalar(
            select(Position)
            .where(Position.id == position_id)
            .with_for_update(read=True, key_share=True)
            .execution_options(populate_existing=True)
        )

    def get_for_schedule_for_update(
        self, schedule_id: int, requirement_id: int
    ) -> ShiftRequirement | None:
        """Lock one requirement belonging to the requested schedule."""
        return self.session.scalar(
            select(ShiftRequirement)
            .where(
                ShiftRequirement.schedule_id == schedule_id,
                ShiftRequirement.id == requirement_id,
            )
            .with_for_update(of=ShiftRequirement)
            .execution_options(populate_existing=True)
        )

    def has_assignments(self, requirement_id: int) -> bool:
        """Check for assignments after the requirement row is locked."""
        return (
            self.session.scalar(
                select(ShiftAssignment.id)
                .where(ShiftAssignment.shift_requirement_id == requirement_id)
                .limit(1)
            )
            is not None
        )

    def add(self, requirement: ShiftRequirement) -> None:
        """Stage a requirement in the caller's transaction."""
        self.session.add(requirement)

    def get_committed(self, requirement_id: int) -> tuple[ShiftRequirement, Position]:
        """Return newly inserted requirement and joined position."""
        return self.session.execute(
            select(ShiftRequirement, Position)
            .join(Position, Position.id == ShiftRequirement.position_id)
            .where(ShiftRequirement.id == requirement_id)
        ).one()

    def list_for_schedule(
        self,
        schedule_id: int,
        *,
        work_date: date | None,
        position_id: int | None,
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[ShiftRequirement, Position]], int]:
        """Return a filtered page and its pre-pagination count."""
        filters = [ShiftRequirement.schedule_id == schedule_id]
        if work_date is not None:
            filters.append(ShiftRequirement.work_date == work_date)
        if position_id is not None:
            filters.append(ShiftRequirement.position_id == position_id)

        total = self.session.scalar(
            select(func.count()).select_from(ShiftRequirement).where(*filters)
        )
        rows = self.session.execute(
            select(ShiftRequirement, Position)
            .join(Position, Position.id == ShiftRequirement.position_id)
            .where(*filters)
            .order_by(
                ShiftRequirement.work_date.asc(),
                ShiftRequirement.start_time.asc(),
                ShiftRequirement.id.asc(),
            )
            .limit(limit)
            .offset(offset)
        ).all()
        return list(rows), total or 0

    def get_for_schedule(
        self, schedule_id: int, requirement_id: int
    ) -> tuple[ShiftRequirement, Position] | None:
        """Return one requirement only when it belongs to the requested schedule."""
        return self.session.execute(
            select(ShiftRequirement, Position)
            .join(Position, Position.id == ShiftRequirement.position_id)
            .where(
                ShiftRequirement.schedule_id == schedule_id,
                ShiftRequirement.id == requirement_id,
            )
        ).one_or_none()
