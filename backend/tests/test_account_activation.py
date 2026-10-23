"""Unit checks for account command schemas and token handling."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr, ValidationError

from src.auth.tokens import generate_action_token, hash_action_token
from src.users.account_service import (
    AccountStateConflict,
    AdministratorRequired,
    InvalidActionToken,
    activate_account,
    create_user,
    reissue_activation_token,
)
from src.users.schemas import ActivateAccountInput, CreateUserInput, IssuedActivation
from src.users.validation import AccountValidationError


def test_token_generation_is_random_and_digest_is_stable() -> None:
    first = generate_action_token()
    second = generate_action_token()

    assert first != second
    assert len(first) == 43
    assert hash_action_token(first) == hash_action_token(first)
    assert hash_action_token(first) != hash_action_token(second)
    assert len(hash_action_token(first)) == 32


@pytest.mark.parametrize(
    ("email", "full_name", "role"),
    [
        ("not-an-email", "Anna Nowak", "employee"),
        ("anna@example.com", " ", "employee"),
        ("anna@example.com", "Anna", "admin"),
        ("anna@example.com", "Anna", "unknown"),
    ],
)
def test_create_user_schema_rejects_bad_input(
    email: str, full_name: str, role: str
) -> None:
    with pytest.raises(ValidationError):
        CreateUserInput(email=email, full_name=full_name, role=role)


def test_create_user_schema_normalizes_fields() -> None:
    data = CreateUserInput(
        email="  ANNA@Example.COM ", full_name=" Anna Nowak ", role="manager"
    )
    assert (data.email, data.full_name) == ("ANNA@example.com", "Anna Nowak")


def test_secret_schemas_hide_values_in_representations() -> None:
    result = IssuedActivation(
        user_id=1,
        activation_token=SecretStr("private-token"),
        expires_at=datetime.now(UTC),
    )
    command = ActivateAccountInput(
        token=SecretStr("private-token"), new_password=SecretStr("private-password")
    )

    assert "private-token" not in repr(result) + repr(command)
    assert "private-password" not in repr(command)


def test_create_user_rolls_back_when_admin_check_fails() -> None:
    session = MagicMock()
    session.scalar.return_value = None
    command = CreateUserInput(
        email="anna@example.com", full_name="Anna Nowak", role="employee"
    )

    with pytest.raises(AdministratorRequired):
        create_user(session, 42, command)

    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()


def test_reissue_rolls_back_when_admin_check_fails() -> None:
    session = MagicMock()
    session.scalar.return_value = None

    with pytest.raises(AdministratorRequired):
        reissue_activation_token(session, 42, 15)

    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()


def test_reissue_rejects_activated_account_before_token_lookup() -> None:
    session = MagicMock()
    session.scalar.side_effect = [
        1,
        MagicMock(is_active=False, activated_at=datetime.now(UTC)),
    ]

    with pytest.raises(AccountStateConflict):
        reissue_activation_token(session, 42, 15)

    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()
    assert session.scalar.call_count == 2


def test_invalid_activation_rolls_back_without_exposing_token() -> None:
    session = MagicMock()
    session.scalar.return_value = None
    command = ActivateAccountInput(
        token=SecretStr("private-token"), new_password=SecretStr("private-password")
    )

    with pytest.raises(InvalidActionToken) as failure:
        activate_account(session, command)

    assert "private-token" not in str(failure.value)
    assert "private-password" not in str(failure.value)
    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()


def test_empty_new_password_is_rejected_before_token_lookup() -> None:
    session = MagicMock()
    command = ActivateAccountInput(
        token=SecretStr("private-token"), new_password=SecretStr("")
    )

    with pytest.raises(AccountValidationError):
        activate_account(session, command)

    session.scalar.assert_not_called()
    session.rollback.assert_called_once_with()
