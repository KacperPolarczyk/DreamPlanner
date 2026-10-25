"""Availability submission and interval ORM models."""

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
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base

if TYPE_CHECKING:
    from src.schedules.models import Schedule
    from src.users.models import User


class AvailabilitySubmission(Base):
    """One user's monthly availability submission."""

    __tablename__ = "availability_submissions"
    __table_args__ = (
        UniqueConstraint("schedule_id", "user_id", name="uq_availability_submission"),
        Index("ix_availability_submissions_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    schedule_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    schedule: Mapped[Schedule] = relationship()
    user: Mapped[User] = relationship()


class AvailabilityEntry(Base):
    """A local time interval declared available or unavailable."""

    __tablename__ = "availability_entries"
    __table_args__ = (
        CheckConstraint("status IN ('available','unavailable')"),
        CheckConstraint("start_time < end_time", name="chk_availability_same_day"),
        UniqueConstraint(
            "submission_id",
            "work_date",
            "start_time",
            "end_time",
            name="uq_availability_entry",
        ),
        Index("ix_availability_entries_submission_date", "submission_id", "work_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("availability_submissions.id", ondelete="CASCADE"),
        nullable=False,
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    status: Mapped[str] = mapped_column(String(15), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    submission: Mapped[AvailabilitySubmission] = relationship()
