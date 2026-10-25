"""Validate and normalize PostgreSQL URLs for the Psycopg 3 driver."""

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError


def normalize_postgresql_url(value: str) -> URL:
    """Accept PostgreSQL URLs using the default or Psycopg 3 driver.

    Raises:
        ValueError: If the URL is malformed or selects an unsupported driver.
    """
    try:
        url = make_url(value)
        _ = url.port
    except (ArgumentError, ValueError, TypeError):
        raise ValueError("Invalid PostgreSQL database URL") from None

    if url.drivername == "postgresql":
        return url.set(drivername="postgresql+psycopg")
    if url.drivername == "postgresql+psycopg":
        return url
    raise ValueError("Unsupported PostgreSQL database driver")
