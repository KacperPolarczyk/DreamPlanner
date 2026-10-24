"""Public response shapes for availability submission reads."""

from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel


class AvailabilityEntryResponse(BaseModel):
    """An approved public representation of one availability interval."""

    id: str
    work_date: date
    start_time: time
    end_time: time
    status: Literal["available", "unavailable"]
    note: str | None


class AvailabilitySubmissionSummary(BaseModel):
    """Metadata for one monthly submission without interval details."""

    id: str
    schedule_id: str
    user_id: str
    submitted_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AvailabilitySubmissionResponse(AvailabilitySubmissionSummary):
    """A monthly submission and its ordered availability intervals."""

    entries: list[AvailabilityEntryResponse]


class AvailabilitySubmissionListResponse(BaseModel):
    """A visible page of submissions and its filtered total."""

    items: list[AvailabilitySubmissionSummary]
    limit: int
    offset: int
    total: int
