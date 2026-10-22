"""Persistence operations needed by the administrator bootstrap."""

from sqlalchemy import select
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
