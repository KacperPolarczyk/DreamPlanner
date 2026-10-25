"""Database settings and request-scoped session lifecycle tests."""

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

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
        assert isinstance(database.get_engine().pool, QueuePool)
        assert database.get_engine().pool._pre_ping is True
    finally:
        database.get_session_factory.cache_clear()
        database.get_engine().dispose()
        database.get_engine.cache_clear()


def test_engine_hides_bound_parameters_in_database_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep sensitive auth values out of SQLAlchemy exception text."""
    database_url = "postgresql+psycopg://app:password@invalid.invalid/db"
    marker = "unique-sensitive-auth-marker"
    monkeypatch.setenv("DATABASE_URL", database_url)

    def memory_engine(url: URL, **options: object) -> Engine:
        assert url.drivername == "postgresql+psycopg"
        assert url.password == "password"
        return create_engine("sqlite+pysqlite:///:memory:", **options)

    monkeypatch.setattr(database, "create_engine", memory_engine)
    database.get_engine.cache_clear()
    engine = database.get_engine()
    try:
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE sample (value TEXT CHECK (value = 'allowed'))")
            )
            with pytest.raises(IntegrityError) as error:
                connection.execute(
                    text("INSERT INTO sample (value) VALUES (:value)"),
                    {"value": marker},
                )

        assert marker in repr(error.value.params)
        assert marker not in str(error.value)
        assert marker not in repr(error.value)
    finally:
        engine.dispose()
        database.get_engine.cache_clear()


def test_engine_normalizes_terraform_url_without_losing_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        "postgresql://app:secret@db.example/dreamplanner"
        "?sslmode=verify-full&sslrootcert=%2Fetc%2Froot.pem"
    )
    seen: dict[str, object] = {}
    monkeypatch.setenv("DATABASE_URL", source)

    def capture_engine(url: URL, **options: object) -> Engine:
        seen["url"] = url
        seen["options"] = options
        return create_engine("sqlite+pysqlite:///:memory:")

    monkeypatch.setattr(database, "create_engine", capture_engine)
    database.get_engine.cache_clear()
    engine = database.get_engine()
    try:
        url = seen["url"]
        assert isinstance(url, URL)
        assert url.drivername == "postgresql+psycopg"
        assert url.query["sslmode"] == "verify-full"
        assert url.query["sslrootcert"] == "/etc/root.pem"
        assert seen["options"] == {"hide_parameters": True, "pool_pre_ping": True}
    finally:
        engine.dispose()
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
