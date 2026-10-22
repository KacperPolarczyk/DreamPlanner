"""Role and user ORM models from the reference PostgreSQL schema."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class Role(Base):
    """A supported application role."""

    __tablename__ = "roles"
    __table_args__ = (CheckConstraint("code IN ('admin','manager','employee')"),)

    id: Mapped[int] = mapped_column(
        SmallInteger, Identity(always=False), primary_key=True
    )
    code: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)


class User(Base):
    """An account with one role and retained scheduling history."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("length(btrim(email)) > 0", name="users_email_not_blank"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    role_id: Mapped[int] = mapped_column(
        SmallInteger, ForeignKey("roles.id"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    role: Mapped[Role] = relationship()


Index("ux_users_email_lower", func.lower(User.email), unique=True)
