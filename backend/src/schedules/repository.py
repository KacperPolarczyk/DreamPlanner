"""Persistence and read queries for monthly schedules."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.availability.models import AvailabilitySubmission
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
