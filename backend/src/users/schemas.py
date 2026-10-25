"""Input and one-time output shapes for account activation workflows."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator

from src.users.validation import validate_account_fields


class CreateUserInput(BaseModel):
    """A trusted administrator's account creation command."""

    email: str
    full_name: str
    role: Literal["manager", "employee"]

    @model_validator(mode="after")
    def normalize_account_fields(self) -> "CreateUserInput":
        """Apply the approved account field rules before service work."""
        self.email, self.full_name = validate_account_fields(self.email, self.full_name)
        return self


class ActivateAccountInput(BaseModel):
    """A user's activation token and chosen password."""

    token: SecretStr
    new_password: SecretStr


class ReactivateAccountInput(BaseModel):
    """A reactivation token and the replacement password."""

    token: SecretStr
    new_password: SecretStr


class ChangePasswordInput(BaseModel):
    """Current credential and a nonempty replacement supplied by its owner."""

    current_password: SecretStr
    new_password: SecretStr = Field(min_length=1)


class ChangeRoleInput(BaseModel):
    """An administrator-selected role for an existing account."""

    role: Literal["admin", "manager", "employee"]


class IssuedActivation(BaseModel):
    """A token available once to a trusted delivery boundary after commit."""

    user_id: int
    activation_token: SecretStr
    expires_at: datetime


class UserProfile(BaseModel):
    """Public account fields returned by authenticated reads."""

    id: str
    email: str
    full_name: str
    role: Literal["admin", "manager", "employee"]
    is_active: bool
    activated_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UserListResponse(BaseModel):
    """A page of public accounts and the unpaginated filtered count."""

    items: list[UserProfile]
    limit: int
    offset: int
    total: int


class EmployeeListItem(BaseModel):
    """Only the account fields needed to choose an assignment target."""

    user_id: str
    full_name: str


class EmployeeListResponse(BaseModel):
    """A filtered page of employees eligible for a new assignment."""

    items: list[EmployeeListItem]
    limit: int
    offset: int
    total: int


class CreatedUserResponse(UserProfile):
    """Public account fields and the one-time first activation token."""

    activation_token: str
    activation_expires_at: datetime


class ActivationTokenResponse(BaseModel):
    """One-time result of replacing a pending account's activation token."""

    activation_token: str
    expires_at: datetime


class ReactivationResponse(UserProfile):
    """Account fields and a reactivation token delivered once."""

    reactivation_token: str
    expires_at: datetime


class IssuedReactivation(BaseModel):
    """Internal delivery value held only until the issuance response."""

    user_id: int
    reactivation_token: SecretStr
    expires_at: datetime
