"""Persistence operations needed by the administrator bootstrap."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.users.models import Role, User


class UserRepository:
    """Read roles and users and stage new users in one session."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def lock_admin_role(self) -> Role | None:
        """Lock the admin role row to serialize bootstrap attempts."""
        return self.session.scalar(
            select(Role).where(Role.code == "admin").with_for_update()
        )

    def active_admin_exists(self, role_id: int) -> bool:
        """Return whether an active administrator already exists."""
        return (
            self.session.scalar(
                select(User.id)
                .where(User.role_id == role_id, User.is_active.is_(True))
                .limit(1)
            )
            is not None
        )

    def add(self, user: User) -> None:
        """Stage a user without committing the transaction."""
        self.session.add(user)

    def get_role(self, code: str) -> Role | None:
        """Find a role by its public code."""
        return self.session.scalar(select(Role).where(Role.code == code))

    def is_active_admin(self, user_id: int) -> bool:
        """Check the trusted actor's current role and activation state."""
        return (
            self.session.scalar(
                select(User.id)
                .join(Role)
                .where(
                    User.id == user_id,
                    Role.code == "admin",
                    User.is_active.is_(True),
                    User.activated_at.is_not(None),
                )
            )
            is not None
        )

    def email_exists(self, email: str) -> bool:
        """Check the case-insensitive email key before attempting insertion."""
        return (
            self.session.scalar(
                select(User.id).where(func.lower(User.email) == email.lower()).limit(1)
            )
            is not None
        )

    def get_by_email(self, email: str) -> User | None:
        """Find an account by its normalized, case-insensitive email key."""
        return self.session.scalar(
            select(User).where(func.lower(User.email) == email.lower())
        )

    def lock_user(self, user_id: int) -> User | None:
        """Lock an account during token issuance or activation."""
        return self.session.scalar(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
