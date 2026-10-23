"""Authentication and revocable browser-session workflows."""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from email_validator import EmailNotValidError, validate_email
from sqlalchemy.orm import Session

from src.auth.jwt_codec import JwtCodec
from src.auth.models import AuthSession, RefreshToken
from src.auth.passwords import hash_password, verify_password
from src.auth.session_repository import AuthSessionRepository
from src.auth.tokens import generate_refresh_token, hash_refresh_token
from src.users.models import User
from src.users.repository import UserRepository

IDLE_LIFETIME = timedelta(hours=24)
ABSOLUTE_LIFETIME = timedelta(days=7)
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


class InvalidCredentials(Exception):
    """Credentials or account state do not permit login."""


class InvalidSession(Exception):
    """The refresh or access session is not valid."""


@dataclass(frozen=True)
class SessionGrant:
    """A one-time refresh value and the account/session identifiers."""

    user_id: int
    session_id: UUID
    refresh_token: str
    refresh_expires_at: datetime
    access_token: str


@dataclass(frozen=True)
class ActiveIdentity:
    """Current account identity and role read from PostgreSQL."""

    user_id: int
    session_id: UUID
    role: str


def _active_user(user: User | None) -> bool:
    """Check both approved account activation conditions."""
    return bool(user and user.is_active and user.activated_at is not None)


def login(session: Session, email: str, password: str, codec: JwtCodec) -> SessionGrant:
    """Verify credentials and create one independently revocable session.

    Raises:
        InvalidCredentials: If the account cannot authenticate.
    """
    try:
        try:
            normalized = validate_email(
                email.strip(), check_deliverability=False
            ).normalized
        except EmailNotValidError:
            normalized = ""
        user = UserRepository(session).get_by_email(normalized) if normalized else None
        matches = verify_password(
            password, user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
        )
        if not matches or not _active_user(user):
            raise InvalidCredentials("Invalid credentials.")

        now = datetime.now(UTC)
        session_id = uuid4()
        refresh = generate_refresh_token()
        repository = AuthSessionRepository(session)
        repository.add_session(
            AuthSession(
                id=session_id,
                user_id=user.id,
                created_at=now,
                idle_expires_at=now + IDLE_LIFETIME,
                absolute_expires_at=now + ABSOLUTE_LIFETIME,
            ),
            RefreshToken(
                id=uuid4(),
                session_id=session_id,
                token_hash=hash_refresh_token(refresh),
                issued_at=now,
            ),
        )
        access = codec.issue(user.id, session_id)
        session.commit()
        return SessionGrant(user.id, session_id, refresh, now + IDLE_LIFETIME, access)
    except Exception:
        session.rollback()
        raise


def refresh_session(
    session: Session, refresh_token: str, codec: JwtCodec
) -> SessionGrant:
    """Rotate a token atomically, revoking its session on known reuse.

    The session row is the common lock for refresh and logout. The token is
    reloaded after acquiring that lock so a waiting request sees committed
    changes from a concurrent request.

    Raises:
        InvalidSession: If the token or its session is invalid.
    """
    try:
        if not refresh_token:
            raise InvalidSession("Invalid session.")
        repository = AuthSessionRepository(session)
        token_hash = hash_refresh_token(refresh_token)
        session_id = repository.find_token_session_id(token_hash)
        if session_id is None:
            raise InvalidSession("Invalid session.")
        auth_session = repository.lock_session(session_id)
        if auth_session is None:
            raise InvalidSession("Invalid session.")
        token = repository.get_token_fresh(token_hash)
        now = datetime.now(UTC)
        if token is None or token.session_id != auth_session.id:
            raise InvalidSession("Invalid session.")
        if token.used_at is not None:
            if auth_session.revoked_at is None:
                auth_session.revoked_at = now
            session.commit()
            raise InvalidSession("Invalid session.")
        user = repository.get_user_fresh(auth_session.user_id)
        if (
            auth_session.revoked_at is not None
            or now >= auth_session.idle_expires_at
            or now >= auth_session.absolute_expires_at
            or not _active_user(user)
        ):
            raise InvalidSession("Invalid session.")

        replacement = generate_refresh_token()
        token.used_at = now
        session.flush()
        repository.add_token(
            RefreshToken(
                id=uuid4(),
                session_id=auth_session.id,
                token_hash=hash_refresh_token(replacement),
                issued_at=now,
            )
        )
        auth_session.idle_expires_at = min(
            now + IDLE_LIFETIME, auth_session.absolute_expires_at
        )
        access = codec.issue(user.id, auth_session.id)
        session.commit()
        return SessionGrant(
            user.id,
            auth_session.id,
            replacement,
            auth_session.idle_expires_at,
            access,
        )
    except InvalidSession:
        if session.in_transaction():
            session.rollback()
        raise
    except Exception:
        session.rollback()
        raise


def revoke_session(session: Session, session_id: UUID, user_id: int) -> None:
    """Revoke only the caller's locked session and commit before returning.

    Raises:
        InvalidSession: If the current session is no longer valid.
    """
    try:
        repository = AuthSessionRepository(session)
        auth_session = repository.lock_session(session_id)
        now = datetime.now(UTC)
        if (
            auth_session is None
            or auth_session.user_id != user_id
            or auth_session.revoked_at is not None
            or now >= auth_session.idle_expires_at
            or now >= auth_session.absolute_expires_at
        ):
            raise InvalidSession("Invalid session.")
        auth_session.revoked_at = now
        session.commit()
    except Exception:
        session.rollback()
        raise


def validate_session(
    session: Session, session_id: UUID, user_id: int
) -> ActiveIdentity:
    """Check a bearer token's database-backed session and current account.

    This read does not extend idle expiry or commit the request transaction.

    Raises:
        InvalidSession: If the account or session no longer permits access.
    """
    repository = AuthSessionRepository(session)
    auth_session = repository.get_session(session_id)
    now = datetime.now(UTC)
    if (
        auth_session is None
        or auth_session.user_id != user_id
        or auth_session.revoked_at is not None
        or now >= auth_session.idle_expires_at
        or now >= auth_session.absolute_expires_at
    ):
        raise InvalidSession("Invalid session.")
    user = repository.get_user_fresh(user_id)
    if not _active_user(user):
        raise InvalidSession("Invalid session.")
    return ActiveIdentity(user_id, session_id, user.role.code)
