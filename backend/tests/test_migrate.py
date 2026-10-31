"""Prevent the deployment runner from migrating an unexpected target."""

from unittest.mock import MagicMock

import pytest

from src import migrate
from src.migrate import validated_url

URL = (
    "postgresql://dreamplanner_migrator:dummy@"
    "psql-dreamplanner-dev-77076.postgres.database.azure.com:5432/dreamplanner"
    "?sslmode=verify-full&sslrootcert=%2Fetc%2Fssl%2Fcerts%2Fca-certificates.crt"
)


def test_valid_migration_url_uses_psycopg() -> None:
    assert validated_url(URL).drivername == "postgresql+psycopg"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        URL.replace("dreamplanner_migrator", "dreamplanner_app"),
        URL.replace("psql-dreamplanner-dev-77076", "another-server"),
        URL.replace("/dreamplanner?", "/another_database?"),
        URL.replace("verify-full", "require"),
        URL.replace(":5432/", ":5433/"),
        URL + "&host=another-server",
        URL + "&options=-csearch_path=other_schema",
        URL.replace("dummy", ""),
    ],
)
def test_invalid_target_is_rejected(value: str | None) -> None:
    with pytest.raises(ValueError):
        validated_url(value)


def test_lock_contention_prevents_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIGRATION_DATABASE_URL", URL)
    engine = MagicMock()
    connection = engine.connect.return_value
    connection.scalar.return_value = False
    monkeypatch.setattr(migrate, "create_engine", lambda *a, **kw: engine)
    upgrade = MagicMock()
    monkeypatch.setattr(migrate.command, "upgrade", upgrade)
    with pytest.raises(RuntimeError, match="Another automated migration"):
        migrate.migrate()
    upgrade.assert_not_called()
    engine.dispose.assert_called_once()


def test_upgrade_failure_is_not_retried_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIGRATION_DATABASE_URL", URL)
    engine = MagicMock()
    connection = engine.connect.return_value
    connection.scalar.return_value = True
    monkeypatch.setattr(migrate, "create_engine", lambda *a, **kw: engine)
    upgrade = MagicMock(side_effect=RuntimeError("DDL failed"))
    monkeypatch.setattr(migrate.command, "upgrade", upgrade)
    context = MagicMock()
    context.get_current_heads.return_value = ("20261023_02",)
    monkeypatch.setattr(migrate.MigrationContext, "configure", lambda conn: context)
    with pytest.raises(RuntimeError, match="DDL failed"):
        migrate.migrate()
    upgrade.assert_called_once()
    assert "pg_advisory_unlock" in str(connection.execute.call_args.args[0])
    engine.dispose.assert_called_once()


def test_error_output_does_not_disclose_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> None:
        raise ValueError("private connection string password=never-log-this")

    monkeypatch.setattr(migrate, "migrate", fail)
    assert migrate.main() == 1
    output = capsys.readouterr()
    assert "ValueError" in output.err
    assert "never-log-this" not in output.err
