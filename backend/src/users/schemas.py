"""Input and one-time output shapes for account activation workflows."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, SecretStr, model_validator

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


class IssuedActivation(BaseModel):
    """A token available once to a trusted delivery boundary after commit."""

    user_id: int
    activation_token: SecretStr
    expires_at: datetime
