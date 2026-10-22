"""Add authentication sessions and action tokens.

Revision ID: 20261023_02
Revises: 20261023_01
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261023_02"
down_revision = "20261023_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add auth records only after existing account activation is reviewed."""
    op.execute(
        """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM users) THEN
        RAISE EXCEPTION 'Existing users require activation review';
    END IF;
END
$$
"""
    )

    op.add_column(
        "users", sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "chk_users_active_requires_activation",
        "users",
        "NOT is_active OR activated_at IS NOT NULL",
    )

    op.create_table(
        "auth_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "created_at < idle_expires_at AND idle_expires_at <= absolute_expires_at",
            name="chk_auth_session_expiry_order",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auth_sessions_user", "auth_sessions", ["user_id"])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", postgresql.BYTEA(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["auth_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_session", "refresh_tokens", ["session_id"])
    op.create_index(
        "ux_refresh_tokens_session_unused",
        "refresh_tokens",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("used_at IS NULL"),
    )

    op.create_table(
        "account_action_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("token_hash", postgresql.BYTEA(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issued_by", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "purpose IN ('activation','password_reset')",
            name="chk_account_action_token_purpose",
        ),
        sa.CheckConstraint(
            "issued_at < expires_at", name="chk_account_action_token_expiry_order"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["issued_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_account_action_tokens_token_hash"),
    )
    op.create_index(
        "ux_account_action_tokens_user_purpose_unused",
        "account_action_tokens",
        ["user_id", "purpose"],
        unique=True,
        postgresql_where=sa.text("used_at IS NULL AND revoked_at IS NULL"),
    )


def downgrade() -> None:
    """Remove authentication records and the activation marker."""
    op.drop_index(
        "ux_account_action_tokens_user_purpose_unused",
        table_name="account_action_tokens",
    )
    op.drop_table("account_action_tokens")
    op.drop_index("ux_refresh_tokens_session_unused", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_session", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index("ix_auth_sessions_user", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_constraint("chk_users_active_requires_activation", "users", type_="check")
    op.drop_column("users", "activated_at")
