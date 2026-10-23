"""Role and deactivation command behavior on a disposable local database."""

import base64
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.auth.jwt_codec import JwtCodec
from src.auth.models import AccountActionToken, AuthSession, RefreshToken
from src.auth.passwords import hash_password
from src.auth.session_service import (
    InvalidCredentials,
    InvalidSession,
    login,
    refresh_session,
    validate_session,
)
from src.auth.tokens import hash_action_token
from src.core.config import Settings
from src.core.database import Base
from src.users.account_service import (
    AccountNotFound,
    AccountStateConflict,
    AdministratorRequired,
)
from src.users.admin_service import (
    LastAdminConflict,
    SelfActionForbidden,
    change_role,
    deactivate_account,
)
from src.users.models import Role, User
from src.users.repository import UserRepository

NOW = datetime.now(UTC)


@pytest.fixture
def accounts():
    """Create isolated role, account, session and action-token records."""
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def register_btrim(connection, record) -> None:
        """Support the account model's PostgreSQL whitespace check."""
        connection.create_function("btrim", 1, str.strip)

    def restore_utc(target, context) -> None:
        """Restore timezone data that SQLite drops from session timestamps."""
        for name in (
            "created_at",
            "idle_expires_at",
            "absolute_expires_at",
            "revoked_at",
        ):
            value = getattr(target, name)
            if value is not None and value.tzinfo is None:
                setattr(target, name, value.replace(tzinfo=UTC))

    event.listen(AuthSession, "load", restore_utc)
    Base.metadata.create_all(
        engine,
        tables=[
            Role.__table__,
            User.__table__,
            AuthSession.__table__,
            RefreshToken.__table__,
            AccountActionToken.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as session:
        session.add_all(
            [
                Role(id=1, code="admin", name="Administrator"),
                Role(id=2, code="manager", name="Manager"),
                Role(id=3, code="employee", name="Employee"),
            ]
        )
        for user_id, role_id, active in (
            (11, 1, True),
            (12, 1, True),
            (13, 2, True),
            (14, 3, True),
            (15, 3, False),
        ):
            session.add(
                User(
                    id=user_id,
                    role_id=role_id,
                    email=f"user{user_id}@example.com",
                    full_name=f"User {user_id}",
                    password_hash=hash_password("correct-password"),
                    is_active=active,
                    activated_at=NOW if active else None,
                )
            )
        session.commit()
    codec = JwtCodec(
        Settings(
            jwt_signing_key_base64=base64.b64encode(secrets.token_bytes(32)).decode(),
            jwt_current_kid="admin-commands-test-v1",
            jwt_issuer="dreamplanner-admin-commands-test",
            jwt_audience="dreamplanner-admin-commands-api",
        )
    )
    yield factory, codec
    event.remove(AuthSession, "load", restore_utc)
    engine.dispose()


def _login(factory: sessionmaker[Session], user_id: int, codec: JwtCodec):
    """Issue a real account session from its current credential."""
    with factory() as session:
        return login(session, f"user{user_id}@example.com", "correct-password", codec)


def _token(user_id: int, purpose: str, *, used: bool = False) -> AccountActionToken:
    """Create a stored test digest without returning a plaintext token."""
    return AccountActionToken(
        id=uuid4(),
        user_id=user_id,
        purpose=purpose,
        token_hash=hash_action_token(f"opaque-{uuid4()}"),
        issued_at=NOW - timedelta(days=2),
        expires_at=NOW - timedelta(days=1),
        used_at=NOW if used else None,
        issued_by=11,
    )


def test_role_change_is_visible_to_existing_session(accounts) -> None:
    factory, codec = accounts
    grant = _login(factory, 13, codec)
    with factory() as session:
        changed = change_role(session, 11, 13, "admin")
        assert changed.role.code == "admin"
    with factory() as session:
        assert session.get(User, 13).role.code == "admin"
        assert validate_session(session, grant.session_id, 13).role == "admin"
        assert session.get(AuthSession, grant.session_id).revoked_at is None
    with factory() as session:
        changed = change_role(session, 11, 12, "employee")
        assert changed.role.code == "employee"


@pytest.mark.parametrize("command", ["role", "deactivation"])
def test_admin_cannot_modify_own_account(accounts, command) -> None:
    factory, _ = accounts
    with factory() as session:
        with pytest.raises(SelfActionForbidden):
            if command == "role":
                change_role(session, 11, 11, "manager")
            else:
                deactivate_account(session, 11, 11)
        assert not session.in_transaction()
    with factory() as session:
        assert session.get(User, 11).is_active is True
        assert session.get(User, 11).role.code == "admin"


@pytest.mark.parametrize("command", ["role", "deactivation"])
def test_non_admin_is_rejected_in_service(accounts, command) -> None:
    factory, _ = accounts
    with factory() as session:
        with pytest.raises(AdministratorRequired):
            if command == "role":
                change_role(session, 13, 14, "admin")
            else:
                deactivate_account(session, 13, 14)
        assert not session.in_transaction()


@pytest.mark.parametrize("command", ["role", "deactivation"])
def test_last_active_admin_guard_is_checked_under_shared_lock(
    accounts, monkeypatch: pytest.MonkeyPatch, command
) -> None:
    factory, _ = accounts
    calls = []
    original_lock = UserRepository.lock_admin_role

    def lock_and_record(self):
        calls.append("lock")
        return original_lock(self)

    def count_after_lock(self, role_id):
        assert calls == ["lock"]
        return 1

    monkeypatch.setattr(UserRepository, "lock_admin_role", lock_and_record)
    monkeypatch.setattr(UserRepository, "count_active_admins", count_after_lock)
    with factory() as session:
        with pytest.raises(LastAdminConflict):
            if command == "role":
                change_role(session, 11, 12, "manager")
            else:
                deactivate_account(session, 11, 12)
        assert not session.in_transaction()
    with factory() as session:
        assert session.get(User, 12).role.code == "admin"
        assert session.get(User, 12).is_active is True


@pytest.mark.parametrize("target_id", [13, 14])
def test_deactivation_revokes_only_target_sessions_and_reset_token(
    accounts, target_id
) -> None:
    factory, codec = accounts
    first = _login(factory, target_id, codec)
    second = _login(factory, target_id, codec)
    other = _login(factory, 12, codec)
    with factory() as session:
        reset = _token(target_id, "password_reset")
        activation = _token(target_id, "activation")
        other_reset = _token(12, "password_reset")
        session.add_all([reset, activation, other_reset])
        session.commit()
        reset_id, activation_id, other_reset_id = (
            reset.id,
            activation.id,
            other_reset.id,
        )

    with factory() as session:
        target = deactivate_account(session, 11, target_id)
        assert target.is_active is False
        assert target.activated_at is not None
    with factory() as session:
        assert session.get(User, target_id).is_active is False
        for grant in (first, second):
            assert session.get(AuthSession, grant.session_id).revoked_at is not None
            with pytest.raises(InvalidSession):
                validate_session(session, grant.session_id, target_id)
            with pytest.raises(InvalidSession):
                refresh_session(session, grant.refresh_token, codec)
        assert session.get(AccountActionToken, reset_id).revoked_at is not None
        assert session.get(AccountActionToken, activation_id).revoked_at is None
        assert session.get(AccountActionToken, other_reset_id).revoked_at is None
        assert session.get(AuthSession, other.session_id).revoked_at is None
    with factory() as session:
        with pytest.raises(InvalidCredentials):
            login(session, f"user{target_id}@example.com", "correct-password", codec)


def test_deactivation_rejects_pending_and_already_inactive_accounts(accounts) -> None:
    factory, _ = accounts
    with factory() as session:
        with pytest.raises(AccountStateConflict):
            deactivate_account(session, 11, 15)
        assert not session.in_transaction()
    with factory() as session:
        deactivate_account(session, 11, 13)
    with factory() as session:
        with pytest.raises(AccountStateConflict):
            deactivate_account(session, 11, 13)


@pytest.mark.parametrize("command", ["role", "deactivation"])
def test_missing_account_raises_not_found(accounts, command) -> None:
    factory, _ = accounts
    with factory() as session:
        with pytest.raises(AccountNotFound):
            if command == "role":
                change_role(session, 11, 999, "manager")
            else:
                deactivate_account(session, 11, 999)


def test_deactivation_commit_failure_rolls_back_account_and_revocations(
    accounts,
) -> None:
    factory, codec = accounts
    grant = _login(factory, 13, codec)
    with factory() as session:
        reset = _token(13, "password_reset")
        session.add(reset)
        session.commit()
        reset_id = reset.id
    with factory() as session:

        def fail_commit() -> None:
            session.flush()
            raise RuntimeError("simulated commit failure")

        session.commit = fail_commit
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            deactivate_account(session, 11, 13)
        assert not session.in_transaction()
    with factory() as session:
        assert session.get(User, 13).is_active is True
        assert session.get(AuthSession, grant.session_id).revoked_at is None
        assert session.get(AccountActionToken, reset_id).revoked_at is None


def test_role_change_commit_failure_preserves_original_role(accounts) -> None:
    factory, _ = accounts
    with factory() as session:

        def fail_commit() -> None:
            session.flush()
            raise RuntimeError("simulated commit failure")

        session.commit = fail_commit
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            change_role(session, 11, 13, "admin")
        assert not session.in_transaction()
    with factory() as session:
        assert session.get(User, 13).role.code == "manager"
