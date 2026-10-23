"""Database queries for position reads."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.positions.models import Position


class PositionRepository:
    """Read positions using one request-scoped database session."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_id(self, position_id: int) -> Position | None:
        """Return a position regardless of its active state."""
        return self.session.get(Position, position_id)

    def list_positions(
        self,
        *,
        is_active: bool | None,
        query: str | None,
        limit: int,
        offset: int,
        sort: str,
    ) -> tuple[list[Position], int]:
        """Return a deterministic filtered page and its unpaginated count."""
        filters = []
        if is_active is not None:
            filters.append(Position.is_active.is_(is_active))
        if query is not None:
            filters.append(
                func.lower(Position.name).contains(query.lower(), autoescape=True)
            )

        total = self.session.scalar(
            select(func.count()).select_from(Position).where(*filters)
        )
        sort_column = {"name": Position.name, "id": Position.id}[sort.removeprefix("-")]
        order = sort_column.desc() if sort.startswith("-") else sort_column.asc()
        positions = self.session.scalars(
            select(Position)
            .where(*filters)
            .order_by(order, Position.id.asc())
            .limit(limit)
            .offset(offset)
        ).all()
        return list(positions), total or 0
