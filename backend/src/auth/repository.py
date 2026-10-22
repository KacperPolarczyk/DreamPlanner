"""Queries and writes for one-time account action tokens."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth.models import AccountActionToken


class ActionTokenRepository:
    """Access activation records using the operation's existing session."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, token: AccountActionToken) -> None:
        """Stage an action token without committing."""
        self.session.add(token)

    def find_activation_by_hash(self, token_hash: bytes) -> AccountActionToken | None:
        """Find a token for lookup; the account lock follows before mutation."""
        return self.session.scalar(
            select(AccountActionToken).where(
                AccountActionToken.token_hash == token_hash,
                AccountActionToken.purpose == "activation",
            )
        )

    def lock_token(self, token_id: UUID) -> AccountActionToken | None:
        """Lock a token after its account is locked."""
        return self.session.scalar(
            select(AccountActionToken)
            .where(AccountActionToken.id == token_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def lock_unused_activation(self, user_id: int) -> AccountActionToken | None:
        """Find an unreplaced activation token while its account is locked."""
        return self.session.scalar(
            select(AccountActionToken)
            .where(
                AccountActionToken.user_id == user_id,
                AccountActionToken.purpose == "activation",
                AccountActionToken.used_at.is_(None),
                AccountActionToken.revoked_at.is_(None),
            )
            .with_for_update()
        )
