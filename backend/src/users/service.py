"""Application workflow for creating the first active administrator."""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from src.auth.passwords import hash_password
from src.users.models import User
from src.users.repository import UserRepository
from src.users.validation import validate_account_fields


class BootstrapError(Exception):
    """A safe, expected bootstrap failure."""


class BootstrapValidationError(BootstrapError):
    """Bootstrap input does not meet the documented account constraints."""


class ActiveAdministratorExists(BootstrapError):
    """An active administrator already exists."""


def validate_bootstrap_account(email: str, full_name: str) -> tuple[str, str]:
    """Validate and normalize the first administrator's public account fields.

    Raises:
        BootstrapValidationError: If an account field is invalid.
    """
    try:
        return validate_account_fields(email, full_name)
    except ValueError as exc:
        raise BootstrapValidationError(str(exc)) from None


def bootstrap_administrator(
    session: Session, email: str, full_name: str, password: str
) -> None:
    """Create the first active administrator in one database transaction.

    The admin role row lock serializes simultaneous bootstrap attempts. This
    workflow owns the session's commit and rolls back on every failure.

    Args:
        session: One SQLAlchemy session for the operation.
        email: Administrator email address.
        full_name: Administrator's full name.
        password: Administrator's chosen password.

    Raises:
        BootstrapValidationError: If the supplied fields are invalid.
        ActiveAdministratorExists: If an active admin is already present.
        BootstrapError: If the admin role is missing.
    """
    try:
        normalized_email, normalized_name = validate_bootstrap_account(email, full_name)
        if not password:
            raise BootstrapValidationError("Password must not be empty.")
        repository = UserRepository(session)
        role = repository.lock_admin_role()
        if role is None:
            raise BootstrapError("Admin role is missing; apply migrations first.")
        if repository.active_admin_exists(role.id):
            raise ActiveAdministratorExists("An active administrator already exists.")

        repository.add(
            User(
                role_id=role.id,
                email=normalized_email,
                full_name=normalized_name,
                password_hash=hash_password(password),
                is_active=True,
                activated_at=datetime.now(UTC),
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
