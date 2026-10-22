"""Unit checks for password handling and the administrator CLI."""

import getpass
import warnings
from unittest.mock import MagicMock

import pytest

from src.auth.passwords import hash_password, verify_password
from src.users import bootstrap_admin
from src.users.service import (
    BootstrapValidationError,
    bootstrap_administrator,
    validate_bootstrap_account,
)


def test_argon2id_hashes_use_random_salts_and_verify() -> None:
    password = "private value for a test"
    first = hash_password(password)
    second = hash_password(password)

    assert first != second
    assert first.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    assert verify_password(password, first)
    assert not verify_password("wrong", first)
    assert not verify_password(password, "invalid hash")


@pytest.mark.parametrize(
    ("email", "full_name"),
    [
        ("invalid", "Anna Nowak"),
        ("", "Anna Nowak"),
        ("a@example.com", " "),
        ("a@example.com", "x" * 151),
        ("a" * 246 + "@example.com", "Anna Nowak"),
    ],
)
def test_account_validation_rejects_invalid_values(email: str, full_name: str) -> None:
    with pytest.raises(BootstrapValidationError):
        validate_bootstrap_account(email, full_name)


def test_account_validation_normalizes_email_and_name() -> None:
    assert validate_bootstrap_account("  ADMIN@Example.COM ", " Anna Nowak ") == (
        "ADMIN@example.com",
        "Anna Nowak",
    )


def test_service_rolls_back_on_database_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = MagicMock()
    session.scalar.side_effect = RuntimeError("database failure")

    with pytest.raises(RuntimeError, match="database failure"):
        bootstrap_administrator(session, "admin@example.com", "Anna", "secret")

    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()


@pytest.mark.parametrize("passwords", [["secret", "different"], ["", ""]])
def test_cli_rejects_bad_password_without_opening_database(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    passwords: list[str],
) -> None:
    answers = iter(passwords)
    monkeypatch.setattr(bootstrap_admin.getpass, "getpass", lambda _: next(answers))
    monkeypatch.setattr(
        bootstrap_admin,
        "get_session_factory",
        lambda: pytest.fail("Database should not be opened"),
    )

    with pytest.raises(SystemExit) as result:
        bootstrap_admin.main(["--email", "admin@example.com", "--full-name", "Anna"])

    output = capsys.readouterr()
    assert result.value.code == 1
    assert all(value not in output.out + output.err for value in passwords if value)


def test_cli_uses_one_session_and_never_prints_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "a secret for this test"
    session = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = session
    factory = MagicMock(return_value=context)
    captured: list[object] = []
    answers = iter([secret, secret])
    monkeypatch.setattr(bootstrap_admin.getpass, "getpass", lambda _: next(answers))
    monkeypatch.setattr(bootstrap_admin, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        bootstrap_admin,
        "bootstrap_administrator",
        lambda *args: captured.extend(args),
    )

    assert (
        bootstrap_admin.main(["--email", "admin@example.com", "--full-name", "Anna"])
        == 0
    )

    assert captured == [session, "admin@example.com", "Anna", secret]
    factory.assert_called_once_with()
    output = capsys.readouterr()
    assert secret not in output.out + output.err


def test_cli_hides_database_errors_and_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "highly secret test value"
    answers = iter([secret, secret])
    monkeypatch.setattr(bootstrap_admin.getpass, "getpass", lambda _: next(answers))
    monkeypatch.setattr(
        bootstrap_admin,
        "get_session_factory",
        lambda: (_ for _ in ()).throw(ValueError(secret)),
    )

    with pytest.raises(SystemExit) as result:
        bootstrap_admin.main(["--email", "admin@example.com", "--full-name", "Anna"])

    output = capsys.readouterr()
    assert result.value.code == 1
    assert secret not in output.out + output.err


def test_cli_rejects_password_argument_without_echoing_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "secret supplied by mistake"
    with pytest.raises(SystemExit) as result:
        bootstrap_admin.main(
            [
                "--email",
                "admin@example.com",
                "--full-name",
                "Anna",
                "--password",
                secret,
            ]
        )

    output = capsys.readouterr()
    assert result.value.code == 2
    assert secret not in output.out + output.err


def test_cli_aborts_if_getpass_would_echo_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unsafe_prompt(_: str) -> str:
        warnings.warn("Input may be echoed.", getpass.GetPassWarning)
        return "secret"

    monkeypatch.setattr(bootstrap_admin.getpass, "getpass", unsafe_prompt)
    with pytest.raises(SystemExit) as result:
        bootstrap_admin.main(["--email", "admin@example.com", "--full-name", "Anna"])

    assert result.value.code == 1
    assert "secret" not in capsys.readouterr().err
