"""Public request and response shapes for authentication endpoints."""

from typing import Literal

from pydantic import BaseModel, Field, SecretStr


class LoginInput(BaseModel):
    """Credentials supplied to create a browser session."""

    email: str = Field(min_length=1, max_length=255)
    password: SecretStr


class AccessTokenResponse(BaseModel):
    """The short-lived access token returned after login or rotation."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: Literal[600] = 600


class ProblemDetails(BaseModel):
    """Safe shared shape for HTTP problem responses."""

    type: str
    title: str
    status: int
    detail: str
