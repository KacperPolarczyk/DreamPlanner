"""Public response shapes for monthly schedule reads."""

import re
from datetime import date, datetime, time
from typing import Literal
from unicodedata import category

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ScheduleStatus = Literal[
    "draft", "collecting", "collection_closed", "closed", "published"
]


class CreateScheduleInput(BaseModel):
    """The exact year and month requested for a new schedule."""

    model_config = ConfigDict(extra="forbid")

    year: int = Field(strict=True, ge=2000, le=2100)
    month: int = Field(strict=True, ge=1, le=12)


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


class ShiftRequirementResponse(BaseModel):
    """The approved public fields of one staffing requirement."""

    id: str
    schedule_id: str
    position_id: str
    position_name: str
    position_is_active: bool
    work_date: date
    start_time: time
    end_time: time
    required_workers: int
    note: str | None


class ShiftRequirementListResponse(BaseModel):
    """A filtered page of staffing requirements and its total count."""

    items: list[ShiftRequirementResponse]
    limit: int
    offset: int
    total: int


class CreateShiftRequirementInput(BaseModel):
    """Strict input for creating one staffing requirement."""

    model_config = ConfigDict(extra="forbid")

    position_id: str
    work_date: date
    start_time: time
    end_time: time
    required_workers: int = Field(strict=True, ge=1, le=2147483647)
    note: str | None = None

    @field_validator("position_id", mode="before")
    @classmethod
    def validate_position_id(cls, value: object) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,19}", value):
            raise ValueError("position_id must be a decimal BIGINT string")
        if not 0 < int(value) <= 9223372036854775807:
            raise ValueError("position_id is outside the positive BIGINT range")
        return value

    @field_validator("work_date", mode="before")
    @classmethod
    def validate_work_date(cls, value: object) -> date:
        if not isinstance(value, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value
        ):
            raise ValueError("work_date must use YYYY-MM-DD")
        return date.fromisoformat(value)

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def validate_time(cls, value: object) -> time:
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
        if not value.strip():
            return None
        if any(category(char) == "Cc" for char in value):
            raise ValueError("note contains control characters")
        return value.strip()


class UpdateShiftRequirementInput(CreateShiftRequirementInput):
    """Sparse, non-null structural fields for a staffing requirement PATCH."""

    position_id: str | None = None
    work_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    required_workers: int | None = Field(default=None, strict=True, ge=1, le=2147483647)

    @field_validator("required_workers", mode="before")
    @classmethod
    def required_workers_cannot_be_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("required_workers cannot be null")
        return value

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> "UpdateShiftRequirementInput":
        if not self.model_fields_set:
            raise ValueError("PATCH body must contain at least one field")
        return self
