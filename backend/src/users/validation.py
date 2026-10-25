"""Shared account field validation for trusted account workflows."""

from email_validator import EmailNotValidError, validate_email


class AccountValidationError(ValueError):
    """Account input violates the approved field constraints."""


def validate_account_fields(email: str, full_name: str) -> tuple[str, str]:
    """Normalize and validate an account's email and full name.

    Raises:
        AccountValidationError: If either field is invalid.
    """
    try:
        normalized_email = validate_email(
            email.strip(), check_deliverability=False
        ).normalized
    except EmailNotValidError:
        raise AccountValidationError("Invalid email address.") from None
    if len(normalized_email) > 255:
        raise AccountValidationError("Email address is too long.")

    normalized_name = full_name.strip()
    if not normalized_name or len(normalized_name) > 150:
        raise AccountValidationError("Full name must contain 1 to 150 characters.")
    return normalized_email, normalized_name
