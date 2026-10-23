"""Administrator role changes and account deactivation workflows."""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from src.auth.repository import ActionTokenRepository
from src.auth.session_repository import AuthSessionRepository
from src.users.account_service import (
    AccountNotFound,
    AccountStateConflict,
    AdministratorRequired,
)
from src.users.models import Role, User
from src.users.repository import UserRepository
from src.users.validation import AccountValidationError


class SelfActionForbidden(Exception):
    """An administrator cannot change their own role or deactivate themself."""


class LastAdminConflict(Exception):
    """The operation would remove the last active administrator."""


def _authorized_target(
    users: UserRepository, actor_id: int, target_id: int
) -> tuple[Role, User]:
    """Lock admin membership, check the actor, then lock another account.

    All administrator membership mutations use this order so the active-admin
    count cannot change between checking it and committing the transaction.
    """
    admin_role = users.lock_admin_role()
    if admin_role is None or not users.is_active_admin(actor_id):
        raise AdministratorRequired("An active administrator is required.")
    if target_id == actor_id:
        raise SelfActionForbidden("Administrators cannot modify their own account.")
    target = users.lock_user(target_id)
    if target is None:
        raise AccountNotFound("Account not found.")
    return admin_role, target


def _protect_last_admin(users: UserRepository, admin_role: Role, target: User) -> None:
    """Reject removal of the last active, first-activated administrator."""
    if (
        target.role_id == admin_role.id
        and target.is_active
        and target.activated_at is not None
        and users.count_active_admins(admin_role.id) <= 1
    ):
        raise LastAdminConflict("The last active administrator must remain.")


def change_role(session: Session, actor_id: int, target_id: int, new_role: str) -> User:
    """Change another account's role with serialized admin protection.

    Raises:
        AdministratorRequired: If the actor lost active admin access.
        SelfActionForbidden: If the actor targets their own account.
        AccountNotFound: If the target does not exist.
        LastAdminConflict: If the last active admin would be demoted.
        AccountValidationError: If the requested role is unsupported.
    """
    try:
        users = UserRepository(session)
        admin_role, target = _authorized_target(users, actor_id, target_id)
        if new_role not in {"admin", "manager", "employee"}:
            raise AccountValidationError("Unsupported role.")
        role = users.get_role(new_role)
        if role is None:
            raise AccountValidationError("Unsupported role.")
        if target.role_id == admin_role.id and role.id != admin_role.id:
            _protect_last_admin(users, admin_role, target)
        target.role = role
        target.updated_at = datetime.now(UTC)
        session.commit()
        return target
    except Exception:
        session.rollback()
        raise


def deactivate_account(session: Session, actor_id: int, target_id: int) -> User:
    """Deactivate another account and revoke its sessions and reset tokens.

    Raises:
        AdministratorRequired: If the actor lost active admin access.
        SelfActionForbidden: If the actor targets their own account.
        AccountNotFound: If the target does not exist.
        AccountStateConflict: If the target is not active and activated.
        LastAdminConflict: If the last active admin would be deactivated.
    """
    try:
        users = UserRepository(session)
        admin_role, target = _authorized_target(users, actor_id, target_id)
        if not target.is_active or target.activated_at is None:
            raise AccountStateConflict("Account is not active and activated.")
        _protect_last_admin(users, admin_role, target)
        now = datetime.now(UTC)
        target.is_active = False
        target.updated_at = now
        AuthSessionRepository(session).revoke_user_sessions(target_id, now)
        ActionTokenRepository(session).revoke_unused_password_resets(target_id, now)
        session.commit()
        return target
    except Exception:
        session.rollback()
        raise
