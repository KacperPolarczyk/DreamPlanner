"""Public request and response shapes for positions."""

from unicodedata import category

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CreatePositionInput(BaseModel):
    """An administrator's request to create one position."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(strict=True)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        """Trim and validate the exact name that will be stored."""
        normalized = value.strip()
        if not 1 <= len(normalized) <= 120 or any(
            category(character) == "Cc" for character in normalized
        ):
            raise ValueError("Name must contain 1 to 120 non-control characters.")
        return normalized


class RenamePositionInput(CreatePositionInput):
    """An administrator's request to replace a position's name."""


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
