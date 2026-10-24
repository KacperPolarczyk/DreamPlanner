"""Public response shapes for monthly schedule reads."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

ScheduleStatus = Literal[
    "draft", "collecting", "collection_closed", "closed", "published"
]


class ScheduleResponse(BaseModel):
    """The approved public fields of one monthly schedule."""

    id: str
    year: int
    month: int
    status: ScheduleStatus
    created_by: str
    created_at: datetime
    collection_opened_at: datetime | None
    collection_closed_at: datetime | None
    published_at: datetime | None


class ScheduleListResponse(BaseModel):
    """A filtered page of schedules with the count before pagination."""

    items: list[ScheduleResponse]
    limit: int
    offset: int
    total: int
