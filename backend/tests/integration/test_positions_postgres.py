"""Position integrity and ordered concurrency on an isolated PostgreSQL database."""

import base64
import os
import secrets
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from src.auth import session_service
from src.core.config import Settings
from src.core.database import get_session
from src.main import create_app
from src.positions.models import Position
from src.positions.service import (
    PositionNameConflict,
    create_position,
    rename_position,
    set_position_active,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("TEST_MIGRATION_DATABASE_URL")
        or not os.getenv("TEST_DATABASE_URL"),
        reason="Set both TEST_*_DATABASE_URL values for a disposable local database",
    ),
]

PATH = "/api/v1/positions"


@pytest.fixture(scope="module")
def postgres_factory() -> Iterator[sessionmaker[Session]]:
    """Migrate a fresh local test database, then remove only its test schema."""
    migration_url = os.environ["TEST_MIGRATION_DATABASE_URL"]
    app_url = os.environ["TEST_DATABASE_URL"]
    migration = make_url(migration_url)
    app = make_url(app_url)
    assert migration.drivername == app.drivername == "postgresql+psycopg"
    assert migration.host in {"localhost", "127.0.0.1"}
    assert (migration.host, migration.port, migration.database) == (
        app.host,
        app.port,
        app.database,
    )
    assert migration.database is not None
    assert migration.database.startswith("dreamplanner_test_")
    assert migration.username == "dreamplanner_migration"
    assert app.username == "dreamplanner_app"
    assert migration.database not in {
        make_url(url).database
        for url in (Settings().database_url, Settings().migration_database_url)
        if url
    }

    migration_engine = create_engine(migration_url)
    app_engine = create_engine(
        app_url,
        connect_args={"options": "-c lock_timeout=8000 -c statement_timeout=15000"},
    )
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    upgraded = False
    try:
        with migration_engine.connect() as connection:
            assert set(inspect(connection).get_table_names()) <= {"alembic_version"}
            if inspect(connection).has_table("alembic_version"):
                assert (
                    connection.scalar(text("SELECT count(*) FROM alembic_version")) == 0
                )
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("MIGRATION_DATABASE_URL", migration_url)
            command.upgrade(config, "head")
            upgraded = True
            yield sessionmaker(bind=app_engine, autoflush=False, autocommit=False)
            command.downgrade(config, "base")
            upgraded = False
    finally:
        if upgraded:
            with pytest.MonkeyPatch.context() as patch:
                patch.setenv("MIGRATION_DATABASE_URL", migration_url)
                command.downgrade(config, "base")
        app_engine.dispose()
        migration_engine.dispose()


@pytest.fixture
def position_api(postgres_factory, monkeypatch: pytest.MonkeyPatch):
    """Use the real PostgreSQL session and real Bearer role dependency."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "positions-postgres-test-v1")
    monkeypatch.setenv("JWT_ISSUER", "positions-postgres-test")
    monkeypatch.setenv("JWT_AUDIENCE", "positions-postgres-api")
    app = create_app()
    session_id = uuid4()
    actor = SimpleNamespace(
        id=42,
        is_active=True,
        activated_at=datetime.now(UTC),
        role=SimpleNamespace(code="admin"),
    )
    auth_session = SimpleNamespace(
        user_id=42,
        revoked_at=None,
        idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
        absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
    )

    class AuthRepository:
        def get_session(self, requested_id):
            return auth_session if requested_id == session_id else None

        def get_user_fresh(self, requested_id):
            return actor if requested_id == 42 else None

    monkeypatch.setattr(
        session_service, "AuthSessionRepository", lambda session: AuthRepository()
    )

    def test_session():
        with postgres_factory() as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    yield app, app.state.jwt_codec.issue(42, session_id)
    app.dependency_overrides.clear()


def _add(factory: sessionmaker[Session], name: str, *, active: bool = True) -> int:
    with factory() as session:
        position = Position(name=name, is_active=active)
        session.add(position)
        session.flush()
        position_id = position.id
        session.commit()
    return position_id


def _position(factory: sessionmaker[Session], position_id: int) -> Position:
    with factory() as session:
        position = session.get(Position, position_id)
        assert position is not None
        session.expunge(position)
        return position


def _http_race(first: Callable[[], object], second: Callable[[], object]):
    barrier = Barrier(2)

    def run(operation: Callable[[], object]):
        barrier.wait(timeout=8)
        return operation()

    with ThreadPoolExecutor(max_workers=2) as workers:
        left = workers.submit(run, first)
        right = workers.submit(run, second)
        return left.result(timeout=25), right.result(timeout=25)


def _wait_until_blocked(engine: Engine, blocked_pid: int, holder_pid: int) -> None:
    """Observe PostgreSQL's actual wait graph before releasing the holder."""
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            blockers = connection.scalar(
                text("SELECT pg_blocking_pids(:pid)"), {"pid": blocked_pid}
            )
        if holder_pid in blockers:
            return
        time.sleep(0.02)
    pytest.fail("Second transaction did not wait for the position row lock")


def _ordered_writes(
    factory: sessionmaker[Session],
    first: Callable[[Session], object],
    second: Callable[[Session], object],
) -> tuple[object, object]:
    """Hold the first write before commit until PostgreSQL reports a blocked peer."""
    first_ready = Event()
    second_ready = Event()
    release_first = Event()
    pids: dict[str, int] = {}

    def first_worker():
        with factory() as session:
            pids["first"] = session.scalar(text("SELECT pg_backend_pid()"))

            def pause_before_commit(current: Session) -> None:
                first_ready.set()
                assert release_first.wait(timeout=12)

            from sqlalchemy import event

            event.listen(session, "before_commit", pause_before_commit, once=True)
            result = first(session)
            return result.id, result.name, result.is_active

    def second_worker():
        with factory() as session:
            pids["second"] = session.scalar(text("SELECT pg_backend_pid()"))
            second_ready.set()
            result = second(session)
            return result.id, result.name, result.is_active

    with ThreadPoolExecutor(max_workers=2) as workers:
        first_future = workers.submit(first_worker)
        try:
            assert first_ready.wait(timeout=8)
            second_future = workers.submit(second_worker)
            assert second_ready.wait(timeout=8)
            _wait_until_blocked(factory.kw["bind"], pids["second"], pids["first"])
        finally:
            release_first.set()
        return first_future.result(timeout=20), second_future.result(timeout=20)


def test_concurrent_create_same_name_has_one_201_and_one_409(
    position_api, postgres_factory
) -> None:
    app, token = position_api
    name = "pg-create-race"

    def create():
        with TestClient(app) as client:
            return client.post(
                PATH,
                json={"name": name},
                headers={"Authorization": f"Bearer {token}"},
            )

    responses = _http_race(create, create)
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert (
        next(response for response in responses if response.status_code == 409).json()[
            "type"
        ]
        == "urn:dreamplanner:problem:position-name-conflict"
    )
    assert all("positions_name_key" not in response.text for response in responses)
    with postgres_factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(Position).where(Position.name == name)
            )
            == 1
        )


def test_concurrent_rename_different_rows_has_one_409(
    position_api, postgres_factory
) -> None:
    app, token = position_api
    left_id = _add(postgres_factory, "pg-rename-left")
    right_id = _add(postgres_factory, "pg-rename-right")
    target = "pg-rename-shared"

    def rename(position_id: int):
        with TestClient(app) as client:
            return client.patch(
                f"{PATH}/{position_id}",
                json={"name": target},
                headers={"Authorization": f"Bearer {token}"},
            )

    responses = _http_race(lambda: rename(left_id), lambda: rename(right_id))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert (
        next(response for response in responses if response.status_code == 409).json()[
            "type"
        ]
        == "urn:dreamplanner:problem:position-name-conflict"
    )
    with postgres_factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(Position)
                .where(Position.name == target)
            )
            == 1
        )


def test_same_row_renames_wait_for_for_update(postgres_factory) -> None:
    position_id = _add(postgres_factory, "pg-same-row-original")
    first, second = _ordered_writes(
        postgres_factory,
        lambda session: rename_position(session, position_id, "pg-same-row-first"),
        lambda session: rename_position(session, position_id, "pg-same-row-second"),
    )
    assert first[1] == "pg-same-row-first"
    assert second[1] == "pg-same-row-second"
    assert _position(postgres_factory, position_id).name == "pg-same-row-second"


def test_patch_then_deactivation_preserve_both_changes(postgres_factory) -> None:
    position_id = _add(postgres_factory, "pg-patch-deactivate-original")
    _ordered_writes(
        postgres_factory,
        lambda session: rename_position(
            session, position_id, "pg-patch-deactivate-new"
        ),
        lambda session: set_position_active(session, position_id, is_active=False),
    )
    position = _position(postgres_factory, position_id)
    assert (position.name, position.is_active) == ("pg-patch-deactivate-new", False)


def test_deactivation_then_reactivation_follow_commit_order(postgres_factory) -> None:
    position_id = _add(postgres_factory, "pg-deactivate-reactivate")
    first, second = _ordered_writes(
        postgres_factory,
        lambda session: set_position_active(session, position_id, is_active=False),
        lambda session: set_position_active(session, position_id, is_active=True),
    )
    assert first[2] is False
    assert second[2] is True
    assert _position(postgres_factory, position_id).is_active is True


def test_inactive_name_conflict_and_rollback_allow_next_write(
    position_api, postgres_factory
) -> None:
    app, token = position_api
    _add(postgres_factory, "pg-inactive-owner", active=False)
    position_id = _add(postgres_factory, "pg-reusable-session")
    with TestClient(app) as client:
        response = client.patch(
            f"{PATH}/{position_id}",
            json={"name": "pg-inactive-owner"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 409
    assert response.json()["type"] == "urn:dreamplanner:problem:position-name-conflict"
    assert "positions_name_key" not in response.text
    with postgres_factory() as session:
        with pytest.raises(PositionNameConflict):
            create_position(session, "pg-inactive-owner")
        assert not session.in_transaction()
        created = create_position(session, "pg-after-rollback")
        assert created.name == "pg-after-rollback"
    assert _position(postgres_factory, position_id).name == "pg-reusable-session"
