"""Check authentication ORM metadata against the approved design."""

from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import CreateIndex

from src.auth.models import AccountActionToken, AuthSession, RefreshToken
from src.core.models import Base
from src.users.models import User

TIMESTAMPTZ = "TIMESTAMP WITH TIME ZONE"
EXPECTED_COLUMNS = {
    "auth_sessions": {
        "id": "UUID",
        "user_id": "BIGINT",
        "created_at": TIMESTAMPTZ,
        "idle_expires_at": TIMESTAMPTZ,
        "absolute_expires_at": TIMESTAMPTZ,
        "revoked_at": TIMESTAMPTZ,
    },
    "refresh_tokens": {
        "id": "UUID",
        "session_id": "UUID",
        "token_hash": "BYTEA",
        "issued_at": TIMESTAMPTZ,
        "used_at": TIMESTAMPTZ,
    },
    "account_action_tokens": {
        "id": "UUID",
        "user_id": "BIGINT",
        "purpose": "TEXT",
        "token_hash": "BYTEA",
        "issued_at": TIMESTAMPTZ,
        "expires_at": TIMESTAMPTZ,
        "used_at": TIMESTAMPTZ,
        "revoked_at": TIMESTAMPTZ,
        "issued_by": "BIGINT",
    },
}
EXPECTED_NULLABLE = {
    "auth_sessions": {"revoked_at"},
    "refresh_tokens": {"used_at"},
    "account_action_tokens": {"used_at", "revoked_at", "issued_by"},
}
EXPECTED_FOREIGN_KEYS = {
    "auth_sessions": {("user_id", "users.id")},
    "refresh_tokens": {("session_id", "auth_sessions.id")},
    "account_action_tokens": {
        ("user_id", "users.id"),
        ("issued_by", "users.id"),
    },
}
EXPECTED_CHECKS = {
    "auth_sessions": {
        (
            "chk_auth_session_expiry_order",
            "created_at<idle_expires_atandidle_expires_at<=absolute_expires_at",
        )
    },
    "account_action_tokens": {
        (
            "chk_account_action_token_purpose",
            "purposein('activation','password_reset')",
        ),
        ("chk_account_action_token_expiry_order", "issued_at<expires_at"),
    },
}
EXPECTED_INDEXES = {
    "auth_sessions": {
        "ix_auth_sessions_user": "CREATE INDEX ix_auth_sessions_user "
        "ON auth_sessions (user_id)"
    },
    "refresh_tokens": {
        "ix_refresh_tokens_session": "CREATE INDEX ix_refresh_tokens_session "
        "ON refresh_tokens (session_id)",
        "ux_refresh_tokens_session_unused": "CREATE UNIQUE INDEX "
        "ux_refresh_tokens_session_unused ON refresh_tokens (session_id) "
        "WHERE used_at IS NULL",
    },
    "account_action_tokens": {
        "ux_account_action_tokens_user_purpose_unused": "CREATE UNIQUE INDEX "
        "ux_account_action_tokens_user_purpose_unused "
        "ON account_action_tokens (user_id, purpose) "
        "WHERE used_at IS NULL AND revoked_at IS NULL"
    },
}


def test_auth_tables_and_users_activation_column() -> None:
    """Register only the approved auth records and a nullable activation time."""
    assert set(Base.metadata.tables) == {
        "roles",
        "users",
        "positions",
        "schedules",
        "shift_requirements",
        "availability_submissions",
        "availability_entries",
        "shift_assignments",
        *EXPECTED_COLUMNS,
    }
    dialect = postgresql.dialect()
    assert User.__table__.c.activated_at.type.compile(dialect=dialect) == TIMESTAMPTZ
    assert User.__table__.c.activated_at.nullable
    assert User.__table__.c.activated_at.server_default is None
    for name, expected in EXPECTED_COLUMNS.items():
        table = Base.metadata.tables[name]
        assert {
            column.name: column.type.compile(dialect=dialect) for column in table.c
        } == (expected)
        assert {column.name for column in table.c if column.nullable} == (
            EXPECTED_NULLABLE.get(name, set())
        )
        assert tuple(column.name for column in table.primary_key.columns) == ("id",)
        assert all(column.server_default is None for column in table.c)


def test_auth_foreign_keys_constraints_and_indexes() -> None:
    """Check direct references, one-use uniqueness, and time ordering."""
    dialect = postgresql.dialect()
    for name in EXPECTED_COLUMNS:
        table = Base.metadata.tables[name]
        assert {(fk.parent.name, fk.target_fullname) for fk in table.foreign_keys} == (
            EXPECTED_FOREIGN_KEYS[name]
        )
        assert all(fk.ondelete is None for fk in table.foreign_keys)
        assert {
            (constraint.name, tuple(column.name for column in constraint.columns))
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        } == {
            "refresh_tokens": {("uq_refresh_tokens_token_hash", ("token_hash",))},
            "account_action_tokens": {
                ("uq_account_action_tokens_token_hash", ("token_hash",))
            },
        }.get(name, set())
        assert {
            (constraint.name, "".join(str(constraint.sqltext).split()).lower())
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        } == EXPECTED_CHECKS.get(name, set())
        assert {
            index.name: str(CreateIndex(index).compile(dialect=dialect))
            for index in table.indexes
        } == EXPECTED_INDEXES[name]

    assert {
        (constraint.name, "".join(str(constraint.sqltext).split()).lower())
        for constraint in User.__table__.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == "chk_users_active_requires_activation"
    } == {
        ("chk_users_active_requires_activation", "notis_activeoractivated_atisnotnull")
    }


def test_auth_relationships_resolve_without_ambiguous_user_keys() -> None:
    """Account action issuer and subject must resolve through different FKs."""
    configure_mappers()
    assert {column.name for column in AuthSession.user.property.local_columns} == {
        "user_id"
    }
    assert {column.name for column in RefreshToken.session.property.local_columns} == {
        "session_id"
    }
    assert {
        column.name for column in AccountActionToken.user.property.local_columns
    } == {"user_id"}
    assert {
        column.name for column in AccountActionToken.issuer.property.local_columns
    } == {"issued_by"}
