"""Shift assignment ORM model from the reference PostgreSQL schema."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base

if TYPE_CHECKING:
    from src.schedules.models import ShiftRequirement
    from src.users.models import User


class ShiftAssignment(Base):
    """A user's assignment to a required shift."""

    __tablename__ = "shift_assignments"
    __table_args__ = (
        CheckConstraint("warning_type IN ('unavailable','unknown')"),
        CheckConstraint(
            "(warning_type IS NULL AND warning_acknowledged_at IS NULL) OR "
            "(warning_type IS NOT NULL AND warning_acknowledged_at IS NOT NULL)",
            name="chk_warning_ack_pair",
        ),
        UniqueConstraint("shift_requirement_id", "user_id", name="uq_shift_assignment"),
        Index("ix_shift_assignments_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    shift_requirement_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("shift_requirements.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )
    assigned_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    warning_type: Mapped[str | None] = mapped_column(String(20))
    warning_acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    shift_requirement: Mapped[ShiftRequirement] = relationship()
    user: Mapped[User] = relationship(foreign_keys=[user_id])
    assigner: Mapped[User] = relationship(foreign_keys=[assigned_by])
