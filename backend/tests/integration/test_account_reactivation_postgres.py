"""Reactivation transactions and concurrent token use on disposable PostgreSQL."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from src.auth.models import AccountActionToken, AuthSession
from src.auth.passwords import hash_password, verify_password
from src.auth.session_service import InvalidCredentials, login
from src.auth.tokens import hash_action_token
from src.core.config import Settings
from src.users import account_service
from src.users.account_service import (
    AccountStateConflict,
    InvalidActionToken,
    issue_reactivation_token,
    reactivate_account,
)
from src.users.models import Role, User
from src.users.schemas import ReactivateAccountInput

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("TEST_MIGRATION_DATABASE_URL")
        or not os.getenv("TEST_DATABASE_URL"),
        reason="Set both TEST_*_DATABASE_URL values for a disposable local database",
    ),
]


def _consume(factory: sessionmaker[Session], token: str, password: str) -> None:
    with factory() as session:
        reactivate_account(
            session,
            ReactivateAccountInput(
                token=SecretStr(token), new_password=SecretStr(password)
            ),
        )


def _check_disposable_urls(migration_url: str, app_url: str) -> None:
    """Allow schema changes only on a separate local disposable test database."""
    migration = make_url(migration_url)
    app = make_url(app_url)
    assert migration.drivername == app.drivername == "postgresql+psycopg"
    assert migration.host in {"localhost", "127.0.0.1"}
    assert (migration.host, migration.port, migration.database) == (
        app.host,
        app.port,
        app.database,
    )
    assert migration.database is not None and migration.database.startswith(
        "dreamplanner_test_"
    )
    assert migration.username == "dreamplanner_migration"
    assert app.username == "dreamplanner_app"
    assert migration.database not in {
        make_url(url).database
        for url in (Settings().database_url, Settings().migration_database_url)
        if url
    }


def test_reactivation_on_disposable_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    migration_url = os.environ["TEST_MIGRATION_DATABASE_URL"]
    app_url = os.environ["TEST_DATABASE_URL"]
    _check_disposable_urls(migration_url, app_url)
    migration_engine = create_engine(migration_url)
    app_engine = create_engine(app_url)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    monkeypatch.setenv("MIGRATION_DATABASE_URL", migration_url)
    upgraded = False
    try:
        with migration_engine.connect() as connection:
            assert not inspect(connection).has_table("users")
        command.upgrade(config, "head")
        upgraded = True
        factory = sessionmaker(bind=app_engine, autoflush=False, autocommit=False)
        with factory() as session:
            admin_role = session.scalar(select(Role).where(Role.code == "admin"))
            manager_role = session.scalar(select(Role).where(Role.code == "manager"))
            assert admin_role is not None and manager_role is not None
            admin = User(
                role_id=admin_role.id,
                email="admin@example.com",
                full_name="Admin",
                password_hash=hash_password("admin-password"),
                is_active=True,
                activated_at=datetime.now(UTC),
            )
            target = User(
                role_id=manager_role.id,
                email="manager@example.com",
                full_name="Manager",
                password_hash=hash_password("old-password"),
                is_active=False,
                activated_at=datetime.now(UTC) - timedelta(days=1),
            )
            session.add_all((admin, target))
            session.flush()
            admin_id, target_id = admin.id, target.id
            activated_at = target.activated_at
            old_session = AuthSession(
                id=uuid4(),
                user_id=target_id,
                created_at=datetime.now(UTC) - timedelta(hours=1),
                idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
                absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
            )
            session.add(old_session)
            old_session_id = old_session.id
            session.commit()

        with factory() as session:
            first = issue_reactivation_token(
                session, admin_id, target_id
            ).reactivation_token.get_secret_value()
        with factory() as session:
            user = session.get(User, target_id)
            assert user is not None and not user.is_active
            assert user.activated_at == activated_at
            assert not verify_password("old-password", user.password_hash)
            assert session.get(AuthSession, old_session_id).revoked_at is not None
            stored = session.scalar(
                select(AccountActionToken).where(
                    AccountActionToken.token_hash == hash_action_token(first)
                )
            )
            assert stored is not None and stored.purpose == "password_reset"
            assert stored.expires_at - stored.issued_at == timedelta(hours=24)
            assert stored.token_hash != first.encode()
        with factory() as session:
            second = issue_reactivation_token(
                session, admin_id, target_id
            ).reactivation_token.get_secret_value()
        with pytest.raises(InvalidActionToken):
            _consume(factory, first, "new-password")
        with pytest.raises(InvalidActionToken):
            _consume(factory, "unknown", "new-password")
        with factory() as session:
            old = session.scalar(
                select(AccountActionToken).where(
                    AccountActionToken.token_hash == hash_action_token(first)
                )
            )
            assert old is not None and old.revoked_at is not None

        with factory() as session:
            expired = session.scalar(
                select(AccountActionToken).where(
                    AccountActionToken.token_hash == hash_action_token(second)
                )
            )
            assert expired is not None
            expired.issued_at = datetime.now(UTC) - timedelta(days=2)
            expired.expires_at = datetime.now(UTC) - timedelta(days=1)
            session.commit()
        with pytest.raises(InvalidActionToken):
            _consume(factory, second, "new-password")

        with factory() as session:
            current = issue_reactivation_token(
                session, admin_id, target_id
            ).reactivation_token.get_secret_value()
        with factory() as session:
            token = session.scalar(
                select(AccountActionToken).where(
                    AccountActionToken.token_hash == hash_action_token(current)
                )
            )
            assert token is not None
            token.used_at = datetime.now(UTC)
            session.commit()
        with pytest.raises(InvalidActionToken):
            _consume(factory, current, "new-password")

        with factory() as session:
            activation = AccountActionToken(
                id=uuid4(),
                user_id=target_id,
                purpose="activation",
                token_hash=hash_action_token("wrong-purpose"),
                issued_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=1),
                issued_by=admin_id,
            )
            session.add(activation)
            session.commit()
        with pytest.raises(InvalidActionToken):
            _consume(factory, "wrong-purpose", "new-password")

        with factory() as session:
            race_token = issue_reactivation_token(
                session, admin_id, target_id
            ).reactivation_token.get_secret_value()
        barrier = Barrier(2)

        def concurrent_use() -> str:
            barrier.wait(timeout=5)
            try:
                _consume(factory, race_token, "new-password")
            except InvalidActionToken:
                return "rejected"
            return "reactivated"

        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(lambda _: concurrent_use(), range(2)))
        assert sorted(outcomes) == ["reactivated", "rejected"]
        with pytest.raises(InvalidActionToken):
            _consume(factory, race_token, "third-password")
        with factory() as session:
            user = session.get(User, target_id)
            assert user is not None and user.is_active
            assert user.activated_at == activated_at
            assert user.role.code == "manager"
            assert verify_password("new-password", user.password_hash)
            assert not verify_password("old-password", user.password_hash)
            assert session.get(AuthSession, old_session_id).revoked_at is not None
            with pytest.raises(AccountStateConflict):
                issue_reactivation_token(session, admin_id, target_id)

        codec = SimpleNamespace(issue=lambda user_id, session_id: "test-access-token")
        with factory() as session:
            with pytest.raises(InvalidCredentials):
                login(session, "manager@example.com", "old-password", codec)
        with factory() as session:
            grant = login(session, "manager@example.com", "new-password", codec)
            assert grant.user_id == target_id

        with factory() as session:
            user = session.get(User, target_id)
            assert user is not None
            user.is_active = False
            session.commit()
        with factory() as session:
            rollback_token = issue_reactivation_token(
                session, admin_id, target_id
            ).reactivation_token.get_secret_value()
        with monkeypatch.context() as patch:
            patch.setattr(
                account_service,
                "hash_password",
                lambda _: (_ for _ in ()).throw(RuntimeError("hash failure")),
            )
            with pytest.raises(RuntimeError, match="hash failure"):
                _consume(factory, rollback_token, "rollback-password")
        with factory() as session:
            user = session.get(User, target_id)
            token = session.scalar(
                select(AccountActionToken).where(
                    AccountActionToken.token_hash == hash_action_token(rollback_token)
                )
            )
            assert user is not None and not user.is_active
            assert token is not None and token.used_at is None
    finally:
        if upgraded:
            command.downgrade(config, "base")
        app_engine.dispose()
        migration_engine.dispose()
