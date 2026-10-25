"""Concurrent authentication and administrator workflows on PostgreSQL."""

import base64
import os
import secrets
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from src.auth.jwt_codec import JwtCodec
from src.auth.models import AccountActionToken, AuthSession
from src.auth.passwords import hash_password, verify_password
from src.auth.session_service import (
    InvalidCredentials,
    InvalidSession,
    login,
    refresh_session,
    revoke_all_sessions,
    validate_session,
)
from src.auth.tokens import hash_action_token
from src.core.config import Settings
from src.users.account_service import (
    AccountStateConflict,
    AdministratorRequired,
    InvalidActionToken,
    activate_account,
    create_user,
    reissue_activation_token,
)
from src.users.admin_service import LastAdminConflict, change_role, deactivate_account
from src.users.models import Role, User
from src.users.password_service import change_own_password
from src.users.schemas import ActivateAccountInput, CreateUserInput

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("TEST_MIGRATION_DATABASE_URL")
        or not os.getenv("TEST_DATABASE_URL"),
        reason="Set both TEST_*_DATABASE_URL values for a disposable local database",
    ),
]


@pytest.fixture
def factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    """Migrate only a fresh local test database, then restore its empty schema."""
    migration_url = os.environ["TEST_MIGRATION_DATABASE_URL"]
    app_url = os.environ["TEST_DATABASE_URL"]
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

    migration_engine = create_engine(migration_url)
    app_engine = create_engine(
        app_url,
        connect_args={"options": "-c lock_timeout=10000 -c statement_timeout=30000"},
    )
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    monkeypatch.setenv("MIGRATION_DATABASE_URL", migration_url)
    upgraded = False
    try:
        with migration_engine.connect() as connection:
            assert set(inspect(connection).get_table_names()) <= {"alembic_version"}
            if inspect(connection).has_table("alembic_version"):
                assert (
                    connection.scalar(text("SELECT count(*) FROM alembic_version")) == 0
                )
        command.upgrade(config, "head")
        upgraded = True
        yield sessionmaker(bind=app_engine, autoflush=False, autocommit=False)
    finally:
        if upgraded:
            command.downgrade(config, "base")
        app_engine.dispose()
        migration_engine.dispose()


def _race(
    first: Callable[[], object], second: Callable[[], object]
) -> tuple[object, object]:
    """Start independent operations together and bound waits on their completion."""
    barrier = Barrier(2)

    def run(operation: Callable[[], object]) -> object:
        barrier.wait(timeout=10)
        return operation()

    with ThreadPoolExecutor(max_workers=2) as workers:
        first_result = workers.submit(run, first)
        second_result = workers.submit(run, second)
        return first_result.result(timeout=40), second_result.result(timeout=40)


def _add_user(
    factory: sessionmaker[Session], email: str, role_code: str = "employee"
) -> int:
    """Create one activated account in a committed setup transaction."""
    with factory() as session:
        role = session.scalar(select(Role).where(Role.code == role_code))
        assert role is not None
        user = User(
            role_id=role.id,
            email=email,
            full_name=email,
            password_hash=hash_password("old-password"),
            is_active=True,
            activated_at=datetime.now(UTC),
        )
        session.add(user)
        session.flush()
        user_id = user.id
        session.commit()
    return user_id


def _codec() -> JwtCodec:
    """Use a fresh signing key without relying on development credentials."""
    return JwtCodec(
        Settings(
            jwt_signing_key_base64=base64.b64encode(secrets.token_bytes(32)).decode(),
            jwt_current_kid="concurrency-test",
            jwt_issuer="dreamplanner-concurrency-test",
            jwt_audience="dreamplanner-concurrency-test-api",
        )
    )


@pytest.mark.parametrize("operation", ["demote", "deactivate"])
def test_concurrent_admin_removal_keeps_an_active_admin(
    factory: sessionmaker[Session], operation: str
) -> None:
    """Opposing administrator removals cannot commit a zero-admin state."""
    first_id = _add_user(factory, "first-admin@example.com", "admin")
    second_id = _add_user(factory, "second-admin@example.com", "admin")

    def remove(actor_id: int, target_id: int) -> str:
        with factory() as session:
            try:
                if operation == "demote":
                    change_role(session, actor_id, target_id, "employee")
                else:
                    deactivate_account(session, actor_id, target_id)
            except (AdministratorRequired, LastAdminConflict):
                return "rejected"
            return "committed"

    outcomes = _race(
        lambda: remove(first_id, second_id),
        lambda: remove(second_id, first_id),
    )
    assert sorted(outcomes) == ["committed", "rejected"]
    with factory() as session:
        users = [session.get(User, user_id) for user_id in (first_id, second_id)]
        assert all(user is not None for user in users)
        assert (
            sum(
                user.is_active
                and user.activated_at is not None
                and user.role.code == "admin"
                for user in users
            )
            == 1
        )


def test_refresh_racing_global_logout_leaves_no_live_session(
    factory: sessionmaker[Session],
) -> None:
    """A token rotation cannot outlive an account-wide session revocation."""
    user_id = _add_user(factory, "logout-race@example.com")
    codec = _codec()
    with factory() as session:
        refresh_grant = login(session, "logout-race@example.com", "old-password", codec)
    with factory() as session:
        logout_grant = login(session, "logout-race@example.com", "old-password", codec)

    def rotate() -> str | None:
        with factory() as session:
            try:
                return refresh_session(
                    session, refresh_grant.refresh_token, codec
                ).refresh_token
            except InvalidSession:
                return None

    def logout() -> None:
        with factory() as session:
            revoke_all_sessions(session, user_id, logout_grant.session_id)

    replacement, _ = _race(rotate, logout)
    with factory() as session:
        sessions = session.scalars(
            select(AuthSession).where(AuthSession.user_id == user_id)
        ).all()
        assert {row.id for row in sessions} == {
            refresh_grant.session_id,
            logout_grant.session_id,
        }
        assert all(row.revoked_at is not None for row in sessions)
        for row in sessions:
            with pytest.raises(InvalidSession):
                validate_session(session, row.id, user_id)
    for token in (refresh_grant.refresh_token, logout_grant.refresh_token, replacement):
        if token is not None:
            with factory() as session:
                with pytest.raises(InvalidSession):
                    refresh_session(session, token, codec)


def test_refresh_racing_password_change_preserves_new_credentials(
    factory: sessionmaker[Session],
) -> None:
    """Password replacement revokes old sessions even if refresh overlaps."""
    user_id = _add_user(factory, "password-race@example.com")
    codec = _codec()
    with factory() as session:
        refresh_grant = login(
            session, "password-race@example.com", "old-password", codec
        )
    with factory() as session:
        change_grant = login(
            session, "password-race@example.com", "old-password", codec
        )

    def rotate() -> str | None:
        with factory() as session:
            try:
                return refresh_session(
                    session, refresh_grant.refresh_token, codec
                ).refresh_token
            except InvalidSession:
                return None

    def change() -> None:
        with factory() as session:
            change_own_password(
                session,
                user_id,
                change_grant.session_id,
                "old-password",
                "new-password",
            )

    replacement, _ = _race(rotate, change)
    with factory() as session:
        user = session.get(User, user_id)
        assert user is not None
        assert verify_password("new-password", user.password_hash)
        assert not verify_password("old-password", user.password_hash)
        sessions = session.scalars(
            select(AuthSession).where(AuthSession.user_id == user_id)
        ).all()
        assert len(sessions) == 2
        assert all(row.revoked_at is not None for row in sessions)
    for token in (refresh_grant.refresh_token, change_grant.refresh_token, replacement):
        if token is not None:
            with factory() as session:
                with pytest.raises(InvalidSession):
                    refresh_session(session, token, codec)
    with factory() as session:
        with pytest.raises(InvalidCredentials):
            login(session, "password-race@example.com", "old-password", codec)
    with factory() as session:
        new_grant = login(session, "password-race@example.com", "new-password", codec)
        assert new_grant.user_id == user_id


def test_reissue_racing_old_token_use_has_one_valid_outcome(
    factory: sessionmaker[Session],
) -> None:
    """Reissue and activation serialize without duplicate usable tokens."""
    admin_id = _add_user(factory, "token-admin@example.com", "admin")
    with factory() as session:
        created = create_user(
            session,
            admin_id,
            CreateUserInput(
                email="pending@example.com", full_name="Pending", role="employee"
            ),
        )
    user_id = created.user_id
    old_token = created.activation_token.get_secret_value()

    def reissue() -> str | None:
        with factory() as session:
            try:
                return reissue_activation_token(
                    session, admin_id, user_id
                ).activation_token.get_secret_value()
            except AccountStateConflict:
                return None

    def activate() -> bool:
        with factory() as session:
            try:
                activate_account(
                    session,
                    ActivateAccountInput(
                        token=SecretStr(old_token),
                        new_password=SecretStr("new-password"),
                    ),
                )
            except InvalidActionToken:
                return False
            return True

    replacement, activated = _race(reissue, activate)
    assert (replacement is None) != (activated is False)
    with factory() as session:
        user = session.get(User, user_id)
        old = session.scalar(
            select(AccountActionToken).where(
                AccountActionToken.token_hash == hash_action_token(old_token)
            )
        )
        assert user is not None and old is not None
        tokens = session.scalars(
            select(AccountActionToken).where(AccountActionToken.user_id == user_id)
        ).all()
        if activated:
            assert replacement is None
            assert user.is_active and user.activated_at is not None
            assert old.used_at is not None and old.revoked_at is None
            assert len(tokens) == 1
        else:
            assert replacement is not None
            assert not user.is_active and user.activated_at is None
            assert old.revoked_at is not None and old.used_at is None
            assert len(tokens) == 2
            assert sum(t.used_at is None and t.revoked_at is None for t in tokens) == 1
    with factory() as session:
        with pytest.raises(InvalidActionToken):
            activate_account(
                session,
                ActivateAccountInput(
                    token=SecretStr(old_token), new_password=SecretStr("reused")
                ),
            )
    if replacement is not None:
        with factory() as session:
            activate_account(
                session,
                ActivateAccountInput(
                    token=SecretStr(replacement), new_password=SecretStr("new-password")
                ),
            )
        with factory() as session:
            with pytest.raises(InvalidActionToken):
                activate_account(
                    session,
                    ActivateAccountInput(
                        token=SecretStr(replacement),
                        new_password=SecretStr("reused"),
                    ),
                )
        with factory() as session:
            assert session.get(User, user_id).is_active
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AccountActionToken)
                    .where(
                        AccountActionToken.user_id == user_id,
                        AccountActionToken.used_at.is_(None),
                        AccountActionToken.revoked_at.is_(None),
                    )
                )
                == 0
            )
