"""Alembic URL configuration tests that never connect to PostgreSQL."""

from contextlib import nullcontext
from pathlib import Path
from runpy import run_path
from types import SimpleNamespace

import pytest
import sqlalchemy
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from alembic import context

ENV_PATH = Path(__file__).resolve().parents[1] / "alembic" / "env.py"


@pytest.fixture(autouse=True)
def stub_alembic_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        context,
        "config",
        SimpleNamespace(config_file_name=None),
        raising=False,
    )


def test_alembic_offline_uses_migration_account_and_psycopg3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:app-secret@db.example/app")
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL",
        "postgresql://migrator:migration-secret@db.example/app"
        "?sslmode=verify-full&sslrootcert=%2Fetc%2Froot.pem",
    )
    monkeypatch.setattr(context, "is_offline_mode", lambda: True)
    monkeypatch.setattr(context, "configure", lambda **kwargs: seen.update(kwargs))
    monkeypatch.setattr(context, "begin_transaction", nullcontext)
    monkeypatch.setattr(context, "run_migrations", lambda: None)

    run_path(str(ENV_PATH))

    url = seen["url"]
    assert isinstance(url, URL)
    assert url.drivername == "postgresql+psycopg"
    assert url.username == "migrator"
    assert url.password == "migration-secret"
    assert url.query["sslmode"] == "verify-full"
    assert url.query["sslrootcert"] == "/etc/root.pem"


def test_alembic_online_keeps_null_pool_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql://migrator:secret@db/app")
    monkeypatch.setattr(context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(context, "configure", lambda **kwargs: seen.update(kwargs))
    monkeypatch.setattr(context, "begin_transaction", nullcontext)
    monkeypatch.setattr(context, "run_migrations", lambda: None)

    class Engine:
        def connect(self):
            return nullcontext("fake connection")

        def dispose(self):
            seen["disposed"] = True

    def capture_engine(url: URL, **kwargs: object) -> Engine:
        seen["url"] = url
        seen["engine_options"] = kwargs
        return Engine()

    monkeypatch.setattr(sqlalchemy, "create_engine", capture_engine)
    run_path(str(ENV_PATH))

    assert seen["url"].drivername == "postgresql+psycopg"
    assert seen["engine_options"] == {"poolclass": NullPool}
    assert seen["connection"] == "fake connection"
    assert seen["disposed"] is True


def test_alembic_never_falls_back_to_application_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:secret@db/app")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "")
    monkeypatch.setattr(context, "is_offline_mode", lambda: True)

    with pytest.raises(ValueError, match="MIGRATION_DATABASE_URL must be configured"):
        run_path(str(ENV_PATH))
