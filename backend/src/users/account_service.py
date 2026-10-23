"""Account creation, activation token issuance, and first activation."""

import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.auth.models import AccountActionToken
from src.auth.passwords import hash_password
from src.auth.repository import ActionTokenRepository
from src.auth.session_repository import AuthSessionRepository
from src.auth.tokens import generate_action_token, hash_action_token
from src.users.models import User
from src.users.repository import UserRepository
from src.users.schemas import (
    ActivateAccountInput,
    CreateUserInput,
    IssuedActivation,
    IssuedReactivation,
    ReactivateAccountInput,
)
from src.users.validation import AccountValidationError

ACTIVATION_LIFETIME = timedelta(hours=24)


class AccountOperationError(Exception):
    """A safe account workflow error."""


class AdministratorRequired(AccountOperationError):
    """The actor is not an active administrator."""


class EmailConflict(AccountOperationError):
    """The normalized email is already assigned to another account."""


class AccountNotFound(AccountOperationError):
    """The requested account does not exist."""


class AccountStateConflict(AccountOperationError):
    """The account cannot receive another first-activation token."""


class InvalidActionToken(AccountOperationError):
    """A token is unknown, expired, revoked, used, or no longer applicable."""


def _new_activation_token(
    user_id: int, issued_by: int, now: datetime
) -> tuple[AccountActionToken, str]:
    """Create a stored digest and the one-time plaintext delivery value."""
    plaintext = generate_action_token()
    record = AccountActionToken(
        id=uuid4(),
        user_id=user_id,
        purpose="activation",
        token_hash=hash_action_token(plaintext),
        issued_at=now,
        expires_at=now + ACTIVATION_LIFETIME,
        issued_by=issued_by,
    )
    return record, plaintext


def _email_conflict(exc: IntegrityError) -> bool:
    """Recognize the database's case-insensitive email constraint."""
    diagnostic = getattr(exc.orig, "diag", None)
    return getattr(diagnostic, "constraint_name", None) == "ux_users_email_lower"


def create_user(
    session: Session, actor_id: int, data: CreateUserInput
) -> IssuedActivation:
    """Create an inactive user and its first token atomically.

    The return value exposes the token once to the trusted caller, after
    commit. Delivery belongs to a later authenticated API or notification
    adapter; the database retains only the digest.

    Raises:
        AdministratorRequired: If the actor is not an active admin.
        AccountValidationError: If the role is unsupported.
        EmailConflict: If the email is already assigned.
    """
    try:
        users = UserRepository(session)
        tokens = ActionTokenRepository(session)
        if not users.is_active_admin(actor_id):
            raise AdministratorRequired("An active administrator is required.")
        if data.role not in {"manager", "employee"}:
            raise AccountValidationError("Role must be manager or employee.")
        role = users.get_role(data.role)
        if role is None:
            raise AccountValidationError("Role must be manager or employee.")
        if users.email_exists(data.email):
            raise EmailConflict("Email address is already in use.")

        user = User(
            role_id=role.id,
            email=data.email,
            full_name=data.full_name,
            password_hash=hash_password(secrets.token_urlsafe(32)),
            is_active=False,
            activated_at=None,
        )
        users.add(user)
        session.flush()
        now = datetime.now(UTC)
        token, plaintext = _new_activation_token(user.id, actor_id, now)
        tokens.add(token)
        result = IssuedActivation(
            user_id=user.id,
            activation_token=SecretStr(plaintext),
            expires_at=token.expires_at,
        )
        session.commit()
        return result
    except IntegrityError as exc:
        session.rollback()
        if _email_conflict(exc):
            raise EmailConflict("Email address is already in use.") from None
        raise
    except Exception:
        session.rollback()
        raise


def reissue_activation_token(
    session: Session, actor_id: int, user_id: int
) -> IssuedActivation:
    """Revoke the old token and issue a fresh one for a pending account.

    Raises:
        AdministratorRequired: If the actor is not an active admin.
        AccountNotFound: If no account has the requested ID.
        AccountStateConflict: If the account was already activated.
    """
    try:
        users = UserRepository(session)
        tokens = ActionTokenRepository(session)
        if not users.is_active_admin(actor_id):
            raise AdministratorRequired("An active administrator is required.")
        user = users.lock_user(user_id)
        if user is None:
            raise AccountNotFound("Account not found.")
        if user.is_active or user.activated_at is not None:
            raise AccountStateConflict("Account is not pending activation.")

        now = datetime.now(UTC)
        previous = tokens.lock_unused_activation(user_id)
        if previous is not None:
            previous.revoked_at = now
            session.flush()
        token, plaintext = _new_activation_token(user_id, actor_id, now)
        tokens.add(token)
        result = IssuedActivation(
            user_id=user_id,
            activation_token=SecretStr(plaintext),
            expires_at=token.expires_at,
        )
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise


def activate_account(session: Session, data: ActivateAccountInput) -> None:
    """Consume a valid token and activate its account in one transaction.

    Raises:
        AccountValidationError: If the new password is empty.
        InvalidActionToken: If the token cannot activate a pending account.
    """
    try:
        plaintext = data.token.get_secret_value()
        password = data.new_password.get_secret_value()
        if not password:
            raise AccountValidationError("Password must not be empty.")
        if not plaintext:
            raise InvalidActionToken("Invalid activation token.")

        users = UserRepository(session)
        tokens = ActionTokenRepository(session)
        found = tokens.find_activation_by_hash(hash_action_token(plaintext))
        if found is None:
            raise InvalidActionToken("Invalid activation token.")
        user = users.lock_user(found.user_id)
        if user is None:
            raise InvalidActionToken("Invalid activation token.")
        token = tokens.lock_token(found.id)
        now = datetime.now(UTC)
        if (
            token is None
            or token.used_at is not None
            or token.revoked_at is not None
            or now >= token.expires_at
            or user.is_active
            or user.activated_at is not None
        ):
            raise InvalidActionToken("Invalid activation token.")

        user.password_hash = hash_password(password)
        user.is_active = True
        user.activated_at = now
        token.used_at = now
        session.commit()
    except Exception:
        session.rollback()
        raise


def issue_reactivation_token(
    session: Session, actor_id: int, user_id: int
) -> IssuedReactivation:
    """Replace credentials and pending reset tokens for a deactivated account.

    Raises:
        AdministratorRequired: If the actor lacks current admin access.
        AccountNotFound: If the target account does not exist.
        AccountStateConflict: If the target is not previously activated and inactive.
    """
    try:
        users = UserRepository(session)
        if not users.is_active_admin(actor_id):
            raise AdministratorRequired("An active administrator is required.")
        user = users.lock_user(user_id)
        if user is None:
            raise AccountNotFound("Account not found.")
        if user.is_active or user.activated_at is None:
            raise AccountStateConflict("Account is not deactivated.")

        now = datetime.now(UTC)
        user.password_hash = hash_password(secrets.token_urlsafe(32))
        user.updated_at = now
        AuthSessionRepository(session).revoke_user_sessions(user_id, now)
        tokens = ActionTokenRepository(session)
        tokens.revoke_unused_password_resets(user_id, now)
        plaintext = generate_action_token()
        token = AccountActionToken(
            id=uuid4(),
            user_id=user_id,
            purpose="password_reset",
            token_hash=hash_action_token(plaintext),
            issued_at=now,
            expires_at=now + ACTIVATION_LIFETIME,
            issued_by=actor_id,
        )
        tokens.add(token)
        issued = IssuedReactivation(
            user_id=user_id,
            reactivation_token=SecretStr(plaintext),
            expires_at=token.expires_at,
        )
        session.commit()
        return issued
    except Exception:
        session.rollback()
        raise


def reactivate_account(session: Session, data: ReactivateAccountInput) -> None:
    """Consume a reset-purpose token and restore only account access.

    Raises:
        AccountValidationError: If the new password violates the existing policy.
        InvalidActionToken: If the token is unusable for reactivation.
    """
    try:
        plaintext = data.token.get_secret_value()
        password = data.new_password.get_secret_value()
        if not password:
            raise AccountValidationError("Password must not be empty.")
        if not plaintext:
            raise InvalidActionToken("Invalid action token.")
        users = UserRepository(session)
        tokens = ActionTokenRepository(session)
        found = tokens.find_password_reset_by_hash(hash_action_token(plaintext))
        if found is None:
            raise InvalidActionToken("Invalid action token.")
        user = users.lock_user(found.user_id)
        if user is None:
            raise InvalidActionToken("Invalid action token.")
        token = tokens.lock_token(found.id)
        now = datetime.now(UTC)
        if (
            token is None
            or token.purpose != "password_reset"
            or token.token_hash != hash_action_token(plaintext)
            or token.used_at is not None
            or token.revoked_at is not None
            or now >= token.expires_at
            or user.is_active
            or user.activated_at is None
        ):
            raise InvalidActionToken("Invalid action token.")
        user.password_hash = hash_password(password)
        user.is_active = True
        user.updated_at = now
        token.used_at = now
        session.commit()
    except Exception:
        session.rollback()
        raise
