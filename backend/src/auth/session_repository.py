"""Persistence operations for browser sessions and refresh tokens."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth.models import AuthSession, RefreshToken
from src.users.models import User


class AuthSessionRepository:
    """Read and stage authentication state in the caller's transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add_session(self, auth_session: AuthSession, token: RefreshToken) -> None:
        """Stage a new browser session and its first token."""
        self.session.add_all((auth_session, token))

    def find_token_session_id(self, token_hash: bytes) -> UUID | None:
        """Find a token's session without trusting the token's earlier state."""
        return self.session.scalar(
            select(RefreshToken.session_id).where(RefreshToken.token_hash == token_hash)
        )

    def lock_session(self, session_id: UUID) -> AuthSession | None:
        """Serialize refresh and revocation operations for this session."""
        return self.session.scalar(
            select(AuthSession)
            .where(AuthSession.id == session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def get_token_fresh(self, token_hash: bytes) -> RefreshToken | None:
        """Read the token again after its parent session has been locked."""
        return self.session.scalar(
            select(RefreshToken)
            .where(RefreshToken.token_hash == token_hash)
            .execution_options(populate_existing=True)
        )

    def get_user_fresh(self, user_id: int) -> User | None:
        """Read current account state after acquiring the session lock."""
        return self.session.scalar(
            select(User)
            .where(User.id == user_id)
            .execution_options(populate_existing=True)
        )

    def get_session(self, session_id: UUID) -> AuthSession | None:
        """Read a session for access-token validation."""
        return self.session.get(AuthSession, session_id)

    def add_token(self, token: RefreshToken) -> None:
        """Stage the next token without committing."""
        self.session.add(token)
