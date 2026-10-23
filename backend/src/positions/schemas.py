"""Public response shapes for position reads."""

from pydantic import BaseModel


class PositionResponse(BaseModel):
    """The public fields of one position."""

    id: str
    name: str
    is_active: bool


class PositionListResponse(BaseModel):
    """A filtered page of positions and its total count."""

    items: list[PositionResponse]
    limit: int
    offset: int
    total: int
