"""Position writing workflows and their expected conflicts."""

import sqlite3

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.positions.models import Position
from src.positions.repository import PositionRepository


class PositionNameConflict(Exception):
    """The exact name is already used by an active or inactive position."""


class PositionNotFound(Exception):
    """The requested position does not exist."""


def _name_conflict(exc: IntegrityError) -> bool:
    """Recognize only the existing positions.name unique constraint."""
    original = exc.orig
    diagnostic = getattr(original, "diag", None)
    if getattr(original, "sqlstate", None) == "23505":
        return getattr(diagnostic, "constraint_name", None) == "positions_name_key"
    return isinstance(original, sqlite3.IntegrityError) and str(original) == (
        "UNIQUE constraint failed: positions.name"
    )


def create_position(session: Session, name: str) -> Position:
    """Persist a position and return it only after a successful commit.

    Args:
        session: The request's database session.
        name: The validated and normalized public name.

    Returns:
        The committed, active position.

    Raises:
        PositionNameConflict: If any position already uses the exact name.
        IntegrityError: If another database constraint fails.
    """
    try:
        position = Position(name=name, is_active=True)
        PositionRepository(session).add(position)
        session.flush()
        session.commit()
        return position
    except IntegrityError as exc:
        session.rollback()
        if _name_conflict(exc):
            raise PositionNameConflict("Position name is already in use.") from None
        raise
    except Exception:
        session.rollback()
        raise


def rename_position(session: Session, position_id: int, name: str) -> Position:
    """Lock and rename a position in one committed transaction.

    Raises:
        PositionNotFound: If the position does not exist.
        PositionNameConflict: If another position owns the exact name.
        IntegrityError: If a different database constraint fails.
    """
    try:
        position = PositionRepository(session).get_by_id_for_update(position_id)
        if position is None:
            raise PositionNotFound
        position.name = name
        session.flush()
        session.commit()
        return position
    except IntegrityError as exc:
        session.rollback()
        if _name_conflict(exc):
            raise PositionNameConflict("Position name is already in use.") from None
        raise
    except Exception:
        session.rollback()
        raise
