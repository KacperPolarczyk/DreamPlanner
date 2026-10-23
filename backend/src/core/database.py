"""Synchronous SQLAlchemy engine and request-scoped session dependency."""

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.core.config import Settings


class Base(DeclarativeBase):
    """Shared declarative base for future feature models."""


@lru_cache
def get_engine() -> Engine:
    """Create the application engine when database access is first needed.

    Raises:
        ValueError: If DATABASE_URL is absent or uses another driver.
    """
    database_url = Settings().database_url
    if not database_url:
        raise ValueError("DATABASE_URL must be configured for database access")
    if make_url(database_url).drivername != "postgresql+psycopg":
        raise ValueError("DATABASE_URL must use the postgresql+psycopg driver")
    return create_engine(database_url, hide_parameters=True)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    """Build a synchronous session factory bound to the application engine."""
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)


def get_session() -> Generator[Session, None, None]:
    """Yield one session for a request and close it afterward.

    FastAPI reuses this dependency within a request. Writing services own
    commit and rollback; closing the session never commits changes.

    Yields:
        The request's SQLAlchemy session.
    """
    session_factory = get_session_factory()
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
