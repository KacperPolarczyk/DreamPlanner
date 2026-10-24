"""Public read schemas for shift assignments."""

from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel


class ShiftAssignmentResponse(BaseModel):
    """Stored assignment and its shift interval, without user details."""

    id: str
    schedule_id: str
    shift_requirement_id: str
    user_id: str
    assigned_by: str
    assigned_at: datetime
    warning_type: Literal["unavailable", "unknown"] | None
    warning_acknowledged_at: datetime | None
    work_date: date
    start_time: time
    end_time: time


class ShiftAssignmentListResponse(BaseModel):
    """One filtered assignment page and its pre-pagination count."""

    items: list[ShiftAssignmentResponse]
    limit: int
    offset: int
    total: int
