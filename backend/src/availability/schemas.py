"""Public response shapes for availability submission reads."""

import re
from datetime import date, datetime, time
from typing import Literal
from unicodedata import category

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AvailabilityEntryInput(BaseModel):
    """Strict input for one employee availability interval."""

    model_config = ConfigDict(extra="forbid")

    work_date: date
    start_time: time
    end_time: time
    status: Literal["available", "unavailable"]
    note: str | None = None

    @field_validator("work_date", mode="before")
    @classmethod
    def strict_date(cls, value: object) -> date:
        if not isinstance(value, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value
        ):
            raise ValueError("work_date must use YYYY-MM-DD")
        return date.fromisoformat(value)

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def strict_time(cls, value: object) -> time:
        if not isinstance(value, str) or not re.fullmatch(
            r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]", value
        ):
            raise ValueError("time must use HH:MM:SS")
        return time.fromisoformat(value)

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(category(char) == "Cc" for char in value):
            raise ValueError("note contains control characters")
        return value.strip() or None


class CreateAvailabilitySubmissionInput(BaseModel):
    """Strict body for creating an employee's monthly declaration."""

    model_config = ConfigDict(extra="forbid")

    entries: list[AvailabilityEntryInput]
    submit: bool = Field(default=False, strict=True)


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
