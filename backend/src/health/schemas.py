"""Response schema for the public health check."""

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Describe process liveness without downstream dependency status."""

    status: Literal["ok"]
