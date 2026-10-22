"""Database settings and request-scoped session lifecycle tests."""

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from src.core import database
from src.core.config import Settings
from src.main import create_app


def test_database_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    app_url = "postgresql+psycopg://app:password@localhost/dreamplanner"
    migration_url = "postgresql+psycopg://migration:password@localhost/dreamplanner"
    monkeypatch.setenv("DATABASE_URL", app_url)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", migration_url)

    settings = Settings()

    assert settings.database_url == app_url
    assert settings.migration_database_url == migration_url


def test_health_does_not_connect_to_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        database,
        "get_engine",
        lambda: pytest.fail("Health endpoint opened the database engine"),
    )

    response = TestClient(create_app()).get("/api/v1/health")

    assert response.status_code == 200


def test_engine_is_lazy_and_session_factory_disables_implicit_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://app:password@invalid.invalid/db"
    )
    database.get_engine.cache_clear()
    database.get_session_factory.cache_clear()
    try:
        factory = database.get_session_factory()

        assert factory.kw["autoflush"] is False
        assert factory.kw["autocommit"] is False
        assert factory.kw["bind"] is database.get_engine()
    finally:
        database.get_session_factory.cache_clear()
        database.get_engine().dispose()
        database.get_engine.cache_clear()


def test_missing_database_url_fails_only_when_session_is_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(database, "Settings", lambda: Settings(_env_file=None))
    database.get_engine.cache_clear()

    with pytest.raises(ValueError, match="DATABASE_URL must be configured"):
        database.get_engine()


@pytest.mark.parametrize("raise_error", [False, True])
def test_request_reuses_session_and_closes_without_commit(
    monkeypatch: pytest.MonkeyPatch, raise_error: bool
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    sessions: list[Session] = []

    class TrackedSession(Session):
        """Record session cleanup and writes for this request test."""

        close_count = 0
        commit_count = 0

        def close(self) -> None:
            """Count closures while preserving SQLAlchemy cleanup."""
            self.close_count += 1
            super().close()

        def commit(self) -> None:
            """Count commits while preserving SQLAlchemy behavior."""
            self.commit_count += 1
            super().commit()

    factory = sessionmaker(bind=engine, class_=TrackedSession)

    def record_session() -> Session:
        """Capture a session created by the dependency."""
        session = factory()
        sessions.append(session)
        return session

    monkeypatch.setattr(database, "get_session_factory", lambda: record_session)
    app = FastAPI()

    @app.get("/probe")
    def probe(
        first: Session = Depends(database.get_session),
        second: Session = Depends(database.get_session),
    ) -> dict[str, bool]:
        """Exercise two dependencies and an active local transaction."""
        assert first is second
        first.execute(text("SELECT 1"))
        if raise_error:
            raise HTTPException(status_code=418)
        return {"same_session": first is second}

    try:
        response = TestClient(app).get("/probe")

        assert response.status_code == (418 if raise_error else 200)
        assert len(sessions) == 1
        assert sessions[0].close_count == 1
        assert sessions[0].commit_count == 0
        assert not sessions[0].in_transaction()
    finally:
        engine.dispose()
