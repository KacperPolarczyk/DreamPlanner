"""Account-wide session revocation and own-password command checks."""

import base64
import secrets
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.auth.jwt_codec import JwtCodec
from src.auth.models import AuthSession, RefreshToken
from src.auth.passwords import hash_password, verify_password
from src.auth.session_service import (
    InvalidCredentials,
    InvalidSession,
    login,
    refresh_session,
    revoke_all_sessions,
    validate_session,
)
from src.core.config import Settings
from src.core.database import Base
from src.users.models import Role, User
from src.users.password_service import InvalidCurrentPassword, change_own_password
from src.users.validation import AccountValidationError


@pytest.fixture
def accounts():
    """Supply disposable accounts and sessions with real SQLAlchemy writes."""
    engine = create_engine("sqlite+pysqlite:///:memory:")

    def restore_utc(target, context) -> None:
        """Keep SQLite's naive timestamps compatible with PostgreSQL models."""
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

    @event.listens_for(engine, "connect")
    def register_btrim(connection, record) -> None:
        """Support the PostgreSQL account constraint in this local test DB."""
        connection.create_function("btrim", 1, str.strip)

    Base.metadata.create_all(
        engine,
        tables=[
            Role.__table__,
            User.__table__,
            AuthSession.__table__,
            RefreshToken.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as session:
        session.add(Role(id=1, code="employee", name="Employee"))
        session.add_all(
            [
                User(
                    id=11,
                    role_id=1,
                    email="owner@example.com",
                    full_name="Owner",
                    password_hash=hash_password("old-password"),
                    is_active=True,
                    activated_at=datetime.now(UTC),
                ),
                User(
                    id=12,
                    role_id=1,
                    email="other@example.com",
                    full_name="Other",
                    password_hash=hash_password("other-password"),
                    is_active=True,
                    activated_at=datetime.now(UTC),
                ),
            ]
        )
        session.commit()
    codec = JwtCodec(
        Settings(
            jwt_signing_key_base64=base64.b64encode(secrets.token_bytes(32)).decode(),
            jwt_current_kid="account-test-v1",
            jwt_issuer="dreamplanner-account-test",
            jwt_audience="dreamplanner-account-test-api",
        )
    )
    yield factory, codec
    event.remove(AuthSession, "load", restore_utc)
    engine.dispose()


def _login(factory: sessionmaker[Session], email: str, password: str, codec: JwtCodec):
    """Create one independent browser session in a fresh unit of work."""
    with factory() as session:
        return login(session, email, password, codec)


def test_global_logout_revokes_only_the_callers_sessions(accounts) -> None:
    factory, codec = accounts
    first = _login(factory, "owner@example.com", "old-password", codec)
    second = _login(factory, "owner@example.com", "old-password", codec)
    other = _login(factory, "other@example.com", "other-password", codec)
    with factory() as session:
        original_updated_at = session.get(User, first.user_id).updated_at

    with factory() as session:
        revoke_all_sessions(session, first.user_id, first.session_id)

    with factory() as session:
        assert session.get(User, first.user_id).updated_at == original_updated_at
        for grant in (first, second):
            record = session.get(AuthSession, grant.session_id)
            assert record is not None and record.revoked_at is not None
            with pytest.raises(InvalidSession):
                validate_session(session, grant.session_id, grant.user_id)
            with pytest.raises(InvalidSession):
                refresh_session(session, grant.refresh_token, codec)
        assert session.get(AuthSession, other.session_id).revoked_at is None
        assert validate_session(session, other.session_id, other.user_id).user_id == 12


def test_password_change_replaces_hash_and_ends_all_sessions(accounts) -> None:
    factory, codec = accounts
    first = _login(factory, "owner@example.com", "old-password", codec)
    second = _login(factory, "owner@example.com", "old-password", codec)
    other = _login(factory, "other@example.com", "other-password", codec)
    with factory() as session:
        owner = session.get(User, 11)
        original_created_at = owner.created_at
        original_updated_at = owner.updated_at
        other_updated_at = session.get(User, 12).updated_at

    with factory() as session:
        with pytest.raises(InvalidCurrentPassword):
            change_own_password(
                session, 11, first.session_id, "wrong-password", "new-password"
            )
        assert not session.in_transaction()
    with factory() as session:
        with pytest.raises(AccountValidationError):
            change_own_password(session, 11, first.session_id, "old-password", "")
        assert not session.in_transaction()
    with factory() as session:
        assert session.get(AuthSession, first.session_id).revoked_at is None
        assert session.get(User, 11).updated_at == original_updated_at
        change_own_password(
            session, 11, first.session_id, "old-password", "new-password"
        )

    with factory() as session:
        owner = session.get(User, 11)
        assert verify_password("new-password", owner.password_hash)
        assert not verify_password("old-password", owner.password_hash)
        assert owner.created_at == original_created_at
        assert owner.updated_at > original_updated_at
        assert session.get(User, 12).updated_at == other_updated_at
        assert session.get(AuthSession, other.session_id).revoked_at is None
        for grant in (first, second):
            assert session.get(AuthSession, grant.session_id).revoked_at is not None
            with pytest.raises(InvalidSession):
                validate_session(session, grant.session_id, grant.user_id)
            with pytest.raises(InvalidSession):
                refresh_session(session, grant.refresh_token, codec)
    with factory() as session:
        with pytest.raises(InvalidCredentials):
            login(session, "owner@example.com", "old-password", codec)
    renewed = _login(factory, "owner@example.com", "new-password", codec)
    assert renewed.user_id == 11


@pytest.mark.parametrize("operation", ["logout", "password"])
def test_command_failure_rolls_back_every_database_change(accounts, operation) -> None:
    factory, codec = accounts
    first = _login(factory, "owner@example.com", "old-password", codec)
    second = _login(factory, "owner@example.com", "old-password", codec)
    with factory() as session:
        original_updated_at = session.get(User, 11).updated_at
    with factory() as session:

        def fail_commit() -> None:
            session.flush()
            raise RuntimeError("simulated commit failure")

        session.commit = fail_commit
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            if operation == "logout":
                revoke_all_sessions(session, 11, first.session_id)
            else:
                change_own_password(
                    session, 11, first.session_id, "old-password", "new-password"
                )
        assert not session.in_transaction()

    with factory() as session:
        owner = session.get(User, 11)
        assert verify_password("old-password", owner.password_hash)
        assert owner.updated_at == original_updated_at
        assert all(
            session.get(AuthSession, grant.session_id).revoked_at is None
            for grant in (first, second)
        )


def test_global_logout_rejects_foreign_or_revoked_current_session(accounts) -> None:
    factory, codec = accounts
    owner = _login(factory, "owner@example.com", "old-password", codec)
    other = _login(factory, "other@example.com", "other-password", codec)
    with factory() as session:
        with pytest.raises(InvalidSession):
            revoke_all_sessions(session, owner.user_id, other.session_id)
        assert not session.in_transaction()
    with factory() as session:
        assert session.get(AuthSession, owner.session_id).revoked_at is None
        assert session.get(AuthSession, other.session_id).revoked_at is None
