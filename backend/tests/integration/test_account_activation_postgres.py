"""Test account creation and one-time activation on disposable PostgreSQL."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from src.auth.models import AccountActionToken
from src.auth.passwords import hash_password, verify_password
from src.auth.tokens import hash_action_token
from src.core.config import Settings
from src.users import account_service
from src.users.account_service import (
    AccountStateConflict,
    AdministratorRequired,
    EmailConflict,
    InvalidActionToken,
    activate_account,
    create_user,
    reissue_activation_token,
)
from src.users.models import Role, User
from src.users.schemas import ActivateAccountInput, CreateUserInput

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("TEST_MIGRATION_DATABASE_URL")
        or not os.getenv("TEST_DATABASE_URL"),
        reason="Set both TEST_*_DATABASE_URL values for a disposable local database",
    ),
]


def _check_disposable_urls(migration_url: str, app_url: str) -> None:
    """Reject any database other than an explicitly separate local test DB."""
    migration = make_url(migration_url)
    app = make_url(app_url)
    assert migration.drivername == app.drivername == "postgresql+psycopg"
    assert migration.host in {"localhost", "127.0.0.1"}
    assert (migration.host, migration.port, migration.database) == (
        app.host,
        app.port,
        app.database,
    )
    assert migration.database is not None
    assert migration.database.startswith("dreamplanner_test_")
    assert migration.username == "dreamplanner_migration"
    assert app.username == "dreamplanner_app"
    assert migration.database not in {
        make_url(url).database
        for url in (Settings().database_url, Settings().migration_database_url)
        if url
    }


def _activate(factory: sessionmaker[Session], token: str, password: str) -> None:
    """Activate through a fresh operation session."""
    with factory() as session:
        activate_account(
            session,
            ActivateAccountInput(
                token=SecretStr(token), new_password=SecretStr(password)
            ),
        )


def _issue(
    factory: sessionmaker[Session], admin_id: int, email: str, role: str = "employee"
) -> tuple[int, str]:
    """Create an account and return its one-time token to this trusted test."""
    with factory() as session:
        result = create_user(
            session,
            admin_id,
            CreateUserInput(email=email, full_name="Test User", role=role),
        )
    return result.user_id, result.activation_token.get_secret_value()


def test_account_activation_on_disposable_postgres(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exercise creation, reissue, activation, invalid states, and concurrency."""
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
            role = session.scalar(select(Role).where(Role.code == "admin"))
            assert role is not None
            admin = User(
                role_id=role.id,
                email="admin@example.com",
                full_name="Test Administrator",
                password_hash=hash_password("test-admin-password"),
                is_active=True,
                activated_at=datetime.now(UTC),
            )
            session.add(admin)
            session.flush()
            admin_id = admin.id
            session.commit()

        command_data = CreateUserInput(
            email="worker@example.com", full_name="Worker", role="employee"
        )
        with factory() as session:
            with pytest.raises(AdministratorRequired):
                create_user(session, 999999, command_data)
            assert not session.in_transaction()

        user_id, first = _issue(factory, admin_id, "worker@example.com")
        with factory() as session:
            user = session.get(User, user_id)
            assert user is not None
            created_at = user.created_at
            initial_updated_at = user.updated_at
            assert created_at == initial_updated_at
            assert user.is_active is False
            assert user.activated_at is None
            assert user.role.code == "employee"
            assert user.password_hash.startswith("$argon2id$")
            assert not verify_password("test-admin-password", user.password_hash)
            token = session.scalar(
                select(AccountActionToken).where(AccountActionToken.user_id == user_id)
            )
            assert token is not None
            assert token.purpose == "activation"
            assert token.issued_by == admin_id
            assert token.expires_at - token.issued_at == timedelta(hours=24)
            assert token.token_hash == hash_action_token(first)
            assert token.token_hash != first.encode()

        with factory() as session:
            with pytest.raises(EmailConflict):
                create_user(
                    session,
                    admin_id,
                    CreateUserInput(
                        email="WORKER@EXAMPLE.COM",
                        full_name="Duplicate",
                        role="manager",
                    ),
                )
            assert not session.in_transaction()

        with factory() as session:
            replacement = reissue_activation_token(session, admin_id, user_id)
        second = replacement.activation_token.get_secret_value()
        assert second != first
        with factory() as session:
            assert session.get(User, user_id).updated_at == initial_updated_at
            old = session.scalar(
                select(AccountActionToken).where(
                    AccountActionToken.token_hash == hash_action_token(first)
                )
            )
            assert old is not None and old.revoked_at is not None
            assert old.used_at is None
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AccountActionToken)
                    .where(
                        AccountActionToken.user_id == user_id,
                        AccountActionToken.purpose == "activation",
                        AccountActionToken.used_at.is_(None),
                        AccountActionToken.revoked_at.is_(None),
                    )
                )
                == 1
            )
        with pytest.raises(InvalidActionToken):
            _activate(factory, first, "unused-password")
        with pytest.raises(InvalidActionToken):
            _activate(factory, "invalid-token", "unused-password")

        activated_after = datetime.now(UTC)
        _activate(factory, second, "chosen-password")
        with factory() as session:
            user = session.get(User, user_id)
            assert user is not None
            assert user.is_active is True
            assert user.activated_at is not None
            assert activated_after <= user.activated_at <= datetime.now(UTC)
            assert user.created_at == created_at
            assert user.updated_at == user.activated_at
            assert user.updated_at > initial_updated_at
            assert user.updated_at.utcoffset() == timedelta(0)
            assert verify_password("chosen-password", user.password_hash)
            token = session.scalar(
                select(AccountActionToken).where(
                    AccountActionToken.token_hash == hash_action_token(second)
                )
            )
            assert token is not None and token.used_at is not None
        with pytest.raises(InvalidActionToken):
            _activate(factory, second, "another-password")
        with factory() as session:
            with pytest.raises(AccountStateConflict):
                reissue_activation_token(session, admin_id, user_id)

        expiring_id, expired = _issue(factory, admin_id, "expired@example.com")
        with factory() as session:
            token = session.scalar(
                select(AccountActionToken).where(
                    AccountActionToken.token_hash == hash_action_token(expired)
                )
            )
            assert token is not None
            token.issued_at = datetime.now(UTC) - timedelta(days=2)
            token.expires_at = datetime.now(UTC) - timedelta(days=1)
            session.commit()
        with pytest.raises(InvalidActionToken):
            _activate(factory, expired, "unused-password")
        with factory() as session:
            current = reissue_activation_token(session, admin_id, expiring_id)
        current_value = current.activation_token.get_secret_value()
        with factory() as session:
            old = session.scalar(
                select(AccountActionToken).where(
                    AccountActionToken.token_hash == hash_action_token(expired)
                )
            )
            assert old is not None and old.revoked_at is not None
            token = session.scalar(
                select(AccountActionToken).where(
                    AccountActionToken.token_hash == hash_action_token(current_value)
                )
            )
            assert token is not None
            token.revoked_at = datetime.now(UTC)
            session.commit()
        with pytest.raises(InvalidActionToken):
            _activate(factory, current_value, "unused-password")
        with factory() as session:
            current = reissue_activation_token(session, admin_id, expiring_id)
        used_value = current.activation_token.get_secret_value()
        with factory() as session:
            token = session.scalar(
                select(AccountActionToken).where(
                    AccountActionToken.token_hash == hash_action_token(used_value)
                )
            )
            assert token is not None
            token.used_at = datetime.now(UTC)
            session.commit()
        with pytest.raises(InvalidActionToken):
            _activate(factory, used_value, "unused-password")

        rollback_id, rollback_token = _issue(factory, admin_id, "rollback@example.com")
        with factory() as session:
            rollback_updated_at = session.get(User, rollback_id).updated_at
        with monkeypatch.context() as patch:
            patch.setattr(
                account_service,
                "hash_password",
                lambda _: (_ for _ in ()).throw(RuntimeError("hash failure")),
            )
            with factory() as session:
                with pytest.raises(RuntimeError, match="hash failure"):
                    activate_account(
                        session,
                        ActivateAccountInput(
                            token=SecretStr(rollback_token),
                            new_password=SecretStr("rollback-password"),
                        ),
                    )
                assert not session.in_transaction()
        with factory() as session:
            user = session.get(User, rollback_id)
            assert user is not None and user.is_active is False
            assert user.updated_at == rollback_updated_at
            token = session.scalar(
                select(AccountActionToken).where(
                    AccountActionToken.token_hash == hash_action_token(rollback_token)
                )
            )
            assert token is not None and token.used_at is None

        race_id, race_token = _issue(factory, admin_id, "race@example.com", "manager")
        barrier = Barrier(2)

        def concurrent_activation() -> str:
            barrier.wait(timeout=5)
            try:
                _activate(factory, race_token, "race-password")
            except InvalidActionToken:
                return "rejected"
            return "activated"

        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(lambda _: concurrent_activation(), range(2)))
        assert sorted(outcomes) == ["activated", "rejected"]
        with factory() as session:
            user = session.get(User, race_id)
            assert user is not None and user.is_active is True
            assert user.role.code == "manager"
            token = session.scalar(
                select(AccountActionToken).where(
                    AccountActionToken.token_hash == hash_action_token(race_token)
                )
            )
            assert token is not None and token.used_at is not None

        output = capsys.readouterr()
        for secret in (
            first,
            second,
            expired,
            current_value,
            used_value,
            race_token,
            "chosen-password",
            "rollback-password",
        ):
            assert secret not in output.out + output.err + caplog.text
    finally:
        if upgraded:
            command.downgrade(config, "base")
        app_engine.dispose()
        migration_engine.dispose()
