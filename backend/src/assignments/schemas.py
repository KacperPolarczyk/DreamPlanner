"""Public schemas for shift assignments."""

import re
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CreateShiftAssignmentInput(BaseModel):
    """Strict manager command for assigning one employee to a shift."""

    model_config = ConfigDict(extra="forbid")

    shift_requirement_id: str
    user_id: str
    warning_acknowledged: bool = Field(default=False, strict=True)

    @field_validator("shift_requirement_id", "user_id", mode="before")
    @classmethod
    def positive_bigint_string(cls, value: object) -> str:
        """Accept only a positive decimal BIGINT represented as JSON string."""
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,19}", value):
            raise ValueError("identifier must be a decimal BIGINT string")
        if not 0 < int(value) <= 9223372036854775807:
            raise ValueError("identifier is outside the positive BIGINT range")
        return value


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
