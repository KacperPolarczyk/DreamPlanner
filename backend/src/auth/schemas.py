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


class ValidationIssue(BaseModel):
    """One safe field-level request validation error."""

    location: list[str]
    code: str
    message: str


class ValidationProblemDetails(ProblemDetails):
    """The 422 problem, including field errors when request parsing supplies them."""

    type: Literal["urn:dreamplanner:problem:validation-error"]
    status: Literal[422]
    errors: list[ValidationIssue] = Field(default_factory=list)


def validation_problem_schema() -> dict[str, object]:
    """Inline the validation item so its reference resolves in OpenAPI."""
    schema = ValidationProblemDetails.model_json_schema()
    schema["properties"]["errors"]["items"] = ValidationIssue.model_json_schema()
    schema.pop("$defs", None)
    return schema
