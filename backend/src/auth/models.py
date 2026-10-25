"""Database records for authentication sessions and opaque tokens."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base

if TYPE_CHECKING:
    from src.users.models import User


class AuthSession(Base):
    """A revocable browser session with idle and absolute expiry times."""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        CheckConstraint(
            "created_at < idle_expires_at AND idle_expires_at <= absolute_expires_at",
            name="chk_auth_session_expiry_order",
        ),
        Index("ix_auth_sessions_user", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    idle_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    absolute_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()


class RefreshToken(Base):
    """A hashed refresh token retained for reuse detection."""

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
        Index("ix_refresh_tokens_session", "session_id"),
        Index(
            "ux_refresh_tokens_session_unused",
            "session_id",
            unique=True,
            postgresql_where=text("used_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("auth_sessions.id"), nullable=False
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    session: Mapped[AuthSession] = relationship()


class AccountActionToken(Base):
    """A hashed token for activation or an administrator-issued password reset."""

    __tablename__ = "account_action_tokens"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('activation','password_reset')",
            name="chk_account_action_token_purpose",
        ),
        CheckConstraint(
            "issued_at < expires_at", name="chk_account_action_token_expiry_order"
        ),
        UniqueConstraint("token_hash", name="uq_account_action_tokens_token_hash"),
        Index(
            "ux_account_action_tokens_user_purpose_unused",
            "user_id",
            "purpose",
            unique=True,
            postgresql_where=text("used_at IS NULL AND revoked_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issued_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    issuer: Mapped[User | None] = relationship(foreign_keys=[issued_by])
