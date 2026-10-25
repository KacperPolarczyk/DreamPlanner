"""Verify auth schema changes on a disposable local PostgreSQL database."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import DBAPIError

from alembic import command
from src.core.config import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("TEST_MIGRATION_DATABASE_URL")
        or not os.getenv("TEST_DATABASE_URL"),
        reason="Set both TEST_*_DATABASE_URL values for a disposable local database",
    ),
]

BACKEND_ROOT = Path(__file__).resolve().parents[2]
AUTH_TABLES = {"auth_sessions", "refresh_tokens", "account_action_tokens"}


def _check_test_urls(migration_url: str, app_url: str) -> None:
    """Keep test DDL away from the development and remote databases."""
    migration = make_url(migration_url)
    app = make_url(app_url)
    assert migration.drivername == app.drivername == "postgresql+psycopg"
    assert (migration.host, migration.port) == (app.host, app.port)
    assert migration.host in {"127.0.0.1", "localhost"}
    assert migration.database == app.database
    assert migration.database is not None
    assert migration.database.startswith("dreamplanner_test_")
    assert migration.username == "dreamplanner_migration"
    assert app.username == "dreamplanner_app"
    assert migration.database not in {
        make_url(url).database
        for url in (Settings().database_url, Settings().migration_database_url)
        if url
    }


def _expect_rejection(connection: Connection, statement: str, **values: object) -> None:
    """Roll back an expected PostgreSQL error to a savepoint."""
    with pytest.raises(DBAPIError), connection.begin_nested():
        connection.execute(text(statement), values)


def _check_schema(connection: Connection) -> None:
    """Check types, constraints, and exact partial-index predicates."""
    inspector = inspect(connection)
    assert AUTH_TABLES <= set(inspector.get_table_names())
    users = {column["name"]: column for column in inspector.get_columns("users")}
    assert str(users["activated_at"]["type"]) == "TIMESTAMP"
    assert users["activated_at"]["type"].timezone
    assert users["activated_at"]["nullable"]
    assert users["activated_at"]["default"] is None

    expected_columns = {
        "auth_sessions": {
            "id",
            "user_id",
            "created_at",
            "idle_expires_at",
            "absolute_expires_at",
            "revoked_at",
        },
        "refresh_tokens": {"id", "session_id", "token_hash", "issued_at", "used_at"},
        "account_action_tokens": {
            "id",
            "user_id",
            "purpose",
            "token_hash",
            "issued_at",
            "expires_at",
            "used_at",
            "revoked_at",
            "issued_by",
        },
    }
    for table in AUTH_TABLES:
        columns = {column["name"]: column for column in inspector.get_columns(table)}
        assert set(columns) == expected_columns[table]
        assert str(columns["id"]["type"]) == "UUID"
        assert not columns["id"]["nullable"]
        if "token_hash" in columns:
            assert str(columns["token_hash"]["type"]) == "BYTEA"
            assert not columns["token_hash"]["nullable"]
        assert all(column["default"] is None for column in columns.values())

    foreign_keys = {
        table: {
            (tuple(fk["constrained_columns"]), fk["referred_table"])
            for fk in inspector.get_foreign_keys(table)
        }
        for table in AUTH_TABLES
    }
    assert foreign_keys == {
        "auth_sessions": {(("user_id",), "users")},
        "refresh_tokens": {(("session_id",), "auth_sessions")},
        "account_action_tokens": {
            (("user_id",), "users"),
            (("issued_by",), "users"),
        },
    }
    constraint_names = set(
        connection.execute(
            text(
                "SELECT conname FROM pg_constraint c "
                "JOIN pg_namespace n ON n.oid=c.connamespace "
                "WHERE n.nspname='public'"
            )
        ).scalars()
    )
    assert {
        "chk_users_active_requires_activation",
        "chk_auth_session_expiry_order",
        "chk_account_action_token_purpose",
        "chk_account_action_token_expiry_order",
        "uq_refresh_tokens_token_hash",
        "uq_account_action_tokens_token_hash",
    } <= constraint_names

    indexes = dict(
        connection.execute(
            text("SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='public'")
        ).all()
    )
    assert "(user_id)" in indexes["ix_auth_sessions_user"]
    assert "(session_id)" in indexes["ix_refresh_tokens_session"]
    assert "CREATE UNIQUE INDEX" in indexes["ux_refresh_tokens_session_unused"]
    assert indexes["ux_refresh_tokens_session_unused"].endswith(
        "WHERE (used_at IS NULL)"
    )
    action_index = indexes["ux_account_action_tokens_user_purpose_unused"]
    assert "CREATE UNIQUE INDEX" in action_index
    assert "(user_id, purpose)" in action_index
    assert action_index.endswith("WHERE ((used_at IS NULL) AND (revoked_at IS NULL))")


def _check_constraints_and_types(connection: Connection) -> None:
    """Exercise activation state, expiry order, UUID, BYTEA, and token uniqueness."""
    now = datetime.now(UTC)
    later = now + timedelta(hours=1)
    latest = now + timedelta(days=7)
    transaction = connection.begin()
    try:
        user_id = connection.scalar(
            text(
                "INSERT INTO users(role_id,email,full_name,password_hash,is_active) "
                "VALUES (3,'auth-worker@example.test','Worker','test-hash',false) "
                "RETURNING id"
            )
        )
        _expect_rejection(
            connection,
            "INSERT INTO users(role_id,email,full_name,password_hash) "
            "VALUES (3,'inactive-default@example.test','Invalid','test-hash')",
        )
        _expect_rejection(
            connection,
            "UPDATE users SET is_active=true WHERE id=:user_id",
            user_id=user_id,
        )
        connection.execute(
            text(
                "UPDATE users SET activated_at=:activated,is_active=true WHERE id=:id"
            ),
            {"activated": now, "id": user_id},
        )
        session_id = uuid4()
        connection.execute(
            text(
                "INSERT INTO auth_sessions"
                "(id,user_id,created_at,idle_expires_at,absolute_expires_at) "
                "VALUES (:id,:user_id,:created,:idle,:absolute)"
            ),
            {
                "id": session_id,
                "user_id": user_id,
                "created": now,
                "idle": later,
                "absolute": latest,
            },
        )
        assert (
            connection.scalar(
                text("SELECT id FROM auth_sessions WHERE id=:id"), {"id": session_id}
            )
            == session_id
        )
        _expect_rejection(
            connection,
            "INSERT INTO auth_sessions"
            "(id,user_id,created_at,idle_expires_at,absolute_expires_at) "
            "VALUES (:id,:user_id,:created,:idle,:absolute)",
            id=uuid4(),
            user_id=user_id,
            created=later,
            idle=now,
            absolute=latest,
        )
        first_hash = bytes(range(32))
        connection.execute(
            text(
                "INSERT INTO refresh_tokens(id,session_id,token_hash,issued_at) "
                "VALUES (:id,:session_id,:hash,:issued)"
            ),
            {
                "id": uuid4(),
                "session_id": session_id,
                "hash": first_hash,
                "issued": now,
            },
        )
        assert (
            connection.scalar(
                text("SELECT token_hash FROM refresh_tokens WHERE session_id=:id"),
                {"id": session_id},
            )
            == first_hash
        )
        _expect_rejection(
            connection,
            "INSERT INTO refresh_tokens(id,session_id,token_hash,issued_at) "
            "VALUES (:id,:session_id,:hash,:issued)",
            id=uuid4(),
            session_id=session_id,
            hash=b"another-hash",
            issued=now,
        )
        connection.execute(
            text("UPDATE refresh_tokens SET used_at=:used WHERE session_id=:session"),
            {"used": later, "session": session_id},
        )
        connection.execute(
            text(
                "INSERT INTO refresh_tokens(id,session_id,token_hash,issued_at) "
                "VALUES (:id,:session_id,:hash,:issued)"
            ),
            {
                "id": uuid4(),
                "session_id": session_id,
                "hash": b"new-hash",
                "issued": later,
            },
        )
        _expect_rejection(
            connection,
            "INSERT INTO refresh_tokens(id,session_id,token_hash,issued_at) "
            "VALUES (:id,:session_id,:hash,:issued)",
            id=uuid4(),
            session_id=session_id,
            hash=first_hash,
            issued=later,
        )

        action_id = uuid4()
        connection.execute(
            text(
                "INSERT INTO account_action_tokens"
                "(id,user_id,purpose,token_hash,issued_at,expires_at) "
                "VALUES (:id,:user_id,:purpose,:hash,:issued,:expires)"
            ),
            {
                "id": action_id,
                "user_id": user_id,
                "purpose": "activation",
                "hash": b"activation-hash",
                "issued": now,
                "expires": later,
            },
        )
        assert (
            connection.scalar(
                text("SELECT id FROM account_action_tokens WHERE id=:id"),
                {"id": action_id},
            )
            == action_id
        )
        _expect_rejection(
            connection,
            "INSERT INTO account_action_tokens"
            "(id,user_id,purpose,token_hash,issued_at,expires_at) "
            "VALUES (:id,:user_id,:purpose,:hash,:issued,:expires)",
            id=uuid4(),
            user_id=user_id,
            purpose="other",
            hash=b"bad-purpose",
            issued=now,
            expires=later,
        )
        _expect_rejection(
            connection,
            "INSERT INTO account_action_tokens"
            "(id,user_id,purpose,token_hash,issued_at,expires_at) "
            "VALUES (:id,:user_id,:purpose,:hash,:issued,:expires)",
            id=uuid4(),
            user_id=user_id,
            purpose="password_reset",
            hash=b"bad-expiry",
            issued=later,
            expires=now,
        )
        _expect_rejection(
            connection,
            "INSERT INTO account_action_tokens"
            "(id,user_id,purpose,token_hash,issued_at,expires_at) "
            "VALUES (:id,:user_id,:purpose,:hash,:issued,:expires)",
            id=uuid4(),
            user_id=user_id,
            purpose="activation",
            hash=b"duplicate-active",
            issued=now,
            expires=later,
        )
        connection.execute(
            text("UPDATE account_action_tokens SET revoked_at=:revoked WHERE id=:id"),
            {"revoked": now, "id": action_id},
        )
        connection.execute(
            text(
                "INSERT INTO account_action_tokens"
                "(id,user_id,purpose,token_hash,issued_at,expires_at) "
                "VALUES (:id,:user_id,'activation',:hash,:issued,:expires)"
            ),
            {
                "id": uuid4(),
                "user_id": user_id,
                "hash": b"new-activation",
                "issued": now,
                "expires": later,
            },
        )
        connection.execute(
            text(
                "INSERT INTO account_action_tokens"
                "(id,user_id,purpose,token_hash,issued_at,expires_at) "
                "VALUES (:id,:user_id,'password_reset',:hash,:issued,:expires)"
            ),
            {
                "id": uuid4(),
                "user_id": user_id,
                "hash": b"reset-hash",
                "issued": now,
                "expires": later,
            },
        )
    finally:
        transaction.rollback()


def _check_app_privileges(app_url: str) -> None:
    """Confirm the application account can use every new table."""
    engine = create_engine(app_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_user")) == "dreamplanner_app"
            for table in AUTH_TABLES:
                connection.scalar(text(f"SELECT count(*) FROM {table}"))
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert connection.scalar(
                        text(
                            "SELECT has_table_privilege(current_user,:table,:privilege)"
                        ),
                        {"table": f"public.{table}", "privilege": privilege},
                    )
    finally:
        engine.dispose()


def test_auth_migration_on_disposable_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject legacy users, then verify upgrade and downgrade in a test database."""
    migration_url = os.environ["TEST_MIGRATION_DATABASE_URL"]
    app_url = os.environ["TEST_DATABASE_URL"]
    _check_test_urls(migration_url, app_url)
    engine = create_engine(migration_url)
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    monkeypatch.setenv("MIGRATION_DATABASE_URL", migration_url)
    try:
        with engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
            assert tables <= {"alembic_version"}
            if "alembic_version" in tables:
                assert (
                    connection.scalar(text("SELECT count(*) FROM alembic_version")) == 0
                )
        command.upgrade(config, "20261023_01")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users"
                    "(role_id,email,full_name,password_hash,is_active) "
                    "VALUES (3,'legacy@example.test','Legacy','test-hash',false)"
                )
            )
        with pytest.raises(DBAPIError, match="activation review"):
            command.upgrade(config, "20261023_02")
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == ("20261023_01")
            assert "activated_at" not in {
                column["name"] for column in inspect(connection).get_columns("users")
            }
            assert not (AUTH_TABLES & set(inspect(connection).get_table_names()))
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM users WHERE email='legacy@example.test'")
            )
        command.upgrade(config, "20261023_02")
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == ("20261023_02")
            _check_schema(connection)
        with engine.connect() as connection:
            _check_constraints_and_types(connection)
        _check_app_privileges(app_url)
        command.downgrade(config, "20261023_01")
        with engine.connect() as connection:
            assert not (AUTH_TABLES & set(inspect(connection).get_table_names()))
            assert "activated_at" not in {
                column["name"] for column in inspect(connection).get_columns("users")
            }
    finally:
        with engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
            if "alembic_version" in tables:
                revision = connection.scalar(
                    text("SELECT version_num FROM alembic_version")
                )
            else:
                revision = None
        if revision is not None:
            command.downgrade(config, "base")
        engine.dispose()
