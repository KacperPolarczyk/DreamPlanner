"""Schedule and shift requirement ORM models."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base

if TYPE_CHECKING:
    from src.positions.models import Position
    from src.users.models import User


class Schedule(Base):
    """A monthly work schedule."""

    __tablename__ = "schedules"
    __table_args__ = (
        CheckConstraint("year BETWEEN 2000 AND 2100"),
        CheckConstraint("month BETWEEN 1 AND 12"),
        CheckConstraint(
            "status IN ('draft','collecting','collection_closed','closed','published')"
        ),
        UniqueConstraint("year", "month", name="uq_schedules_period"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=text("'draft'")
    )
    created_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    collection_opened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    collection_closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    creator: Mapped[User] = relationship(foreign_keys=[created_by])


class ShiftRequirement(Base):
    """Required staffing for a position and local time interval."""

    __tablename__ = "shift_requirements"
    __table_args__ = (
        CheckConstraint("required_workers > 0"),
        CheckConstraint("start_time < end_time", name="chk_shift_same_day"),
        UniqueConstraint(
            "schedule_id",
            "position_id",
            "work_date",
            "start_time",
            "end_time",
            name="uq_shift_requirement",
        ),
        Index("ix_shift_requirements_schedule_date", "schedule_id", "work_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    schedule_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False
    )
    position_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("positions.id"), nullable=False
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    required_workers: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    schedule: Mapped[Schedule] = relationship()
    position: Mapped[Position] = relationship()
