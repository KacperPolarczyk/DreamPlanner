"""Own-password change and account-wide session revocation."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from src.auth.passwords import hash_password, verify_password
from src.auth.session_repository import AuthSessionRepository
from src.auth.session_service import lock_current_account
from src.users.validation import AccountValidationError


class InvalidCurrentPassword(Exception):
    """The supplied current password does not match the account hash."""


def change_own_password(
    session: Session,
    user_id: int,
    current_session_id: UUID,
    current_password: str,
    new_password: str,
) -> None:
    """Replace the caller's password and revoke every session atomically.

    Raises:
        AccountValidationError: If the replacement password is empty.
        InvalidCurrentPassword: If the current password is incorrect.
        InvalidSession: If the account or current session is no longer valid.
    """
    try:
        if not new_password:
            raise AccountValidationError("Password must not be empty.")
        user = lock_current_account(session, user_id, current_session_id)
        if not verify_password(current_password, user.password_hash):
            raise InvalidCurrentPassword("Invalid current password.")
        user.password_hash = hash_password(new_password)
        AuthSessionRepository(session).revoke_user_sessions(user_id, datetime.now(UTC))
        session.commit()
    except Exception:
        session.rollback()
        raise
