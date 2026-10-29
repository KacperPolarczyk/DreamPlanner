"""Schedule persistence, referential integrity, and lock ordering on PostgreSQL."""

import base64
import os
import secrets
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from datetime import time as local_time
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.config import Config
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from src.assignments.models import ShiftAssignment
from src.auth import session_service
from src.availability.models import AvailabilityEntry, AvailabilitySubmission
from src.core.config import Settings
from src.core.database import get_session
from src.main import create_app
from src.positions.models import Position
from src.schedules.models import Schedule, ShiftRequirement
from src.schedules.service import SchedulePeriodConflict, create_schedule
from src.users.models import Role, User

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("TEST_MIGRATION_DATABASE_URL")
        or not os.getenv("TEST_DATABASE_URL"),
        reason=(
            "Set both TEST_*_DATABASE_URL values for an isolated PostgreSQL database"
        ),
    ),
]

PATH = "/api/v1/schedules"
DB_LOCK_TIMEOUT = 8
_PERIOD_INDEX = 0


@pytest.fixture(scope="module")
def postgres_factory() -> Iterator[sessionmaker[Session]]:
    """Migrate a verified empty, dedicated local test database for this module."""
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
    assert migration.database == "dreamplanner_test_schedules_98"
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
        connect_args={
            "options": f"-c lock_timeout={DB_LOCK_TIMEOUT * 1000} "
            "-c statement_timeout=20000"
        },
    )
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    upgraded = False
    try:
        with migration_engine.connect() as connection:
            tables = set(
                connection.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                ).scalars()
            )
            assert tables <= {"alembic_version"}, (
                f"Dedicated test database is not empty: {sorted(tables)}"
            )
            if "alembic_version" in tables:
                assert (
                    connection.scalar(text("SELECT count(*) FROM alembic_version")) == 0
                )
            assert connection.scalar(
                text(
                    "SELECT has_schema_privilege('dreamplanner_app', 'public', 'USAGE')"
                )
            )
            active = connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid()"
                )
            )
            assert active == 0, "Dedicated test database is in use by another process"
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
def schedules_api(postgres_factory, monkeypatch: pytest.MonkeyPatch):
    """Create real parent rows and use PostgreSQL-backed request sessions."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "schedules-postgres-test-v1")
    monkeypatch.setenv("JWT_ISSUER", "dreamplanner-schedules-postgres-test")
    monkeypatch.setenv("JWT_AUDIENCE", "dreamplanner-schedules-postgres-api")
    app = create_app()
    session_id = uuid4()
    with postgres_factory() as session:
        manager_role = session.scalar(select(Role).where(Role.code == "manager"))
        employee_role = session.scalar(select(Role).where(Role.code == "employee"))
        assert manager_role is not None and employee_role is not None
        manager = User(
            role_id=manager_role.id,
            email=f"manager-{uuid4()}@example.test",
            full_name="Schedule Test Manager",
            password_hash="unused-test-hash",
            is_active=True,
            activated_at=datetime.now(UTC),
        )
        employee = User(
            role_id=employee_role.id,
            email=f"employee-{uuid4()}@example.test",
            full_name="Schedule Test Employee",
            password_hash="unused-test-hash",
            is_active=True,
            activated_at=datetime.now(UTC),
        )
        position = Position(name=f"Schedule Test Position {uuid4()}", is_active=True)
        session.add_all([manager, employee, position])
        session.commit()
        manager_id, employee_id, position_id = manager.id, employee.id, position.id

    actor = SimpleNamespace(
        id=manager_id,
        is_active=True,
        activated_at=datetime.now(UTC),
        role=SimpleNamespace(code="manager"),
    )
    auth_session = SimpleNamespace(
        user_id=manager_id,
        revoked_at=None,
        idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
        absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
    )

    class AuthRepository:
        """Return the fixture manager for the app's real Bearer checks."""

        def get_session(self, requested_id):
            return auth_session if requested_id == session_id else None

        def get_user_fresh(self, requested_id):
            return actor if requested_id == manager_id else None

    monkeypatch.setattr(
        session_service, "AuthSessionRepository", lambda session: AuthRepository()
    )

    def test_session(request: Request):
        with postgres_factory() as session:
            label = request.headers.get("x-test-request")
            if label:
                _request_session_opened(label, session)
            yield session

    app.dependency_overrides[get_session] = test_session
    token = app.state.jwt_codec.issue(manager_id, session_id)
    yield app, token, actor, postgres_factory, manager_id, employee_id, position_id
    app.dependency_overrides.clear()


_REQUEST_TRACKER_LOCK = Lock()
_REQUEST_PIDS: dict[str, int] = {}
_REQUEST_CONNECTED: dict[str, Event] = {}


def _request_session_opened(label: str, session: Session) -> None:
    """Record independent PostgreSQL backend PIDs for deterministic lock tests."""
    pid = session.scalar(text("SELECT pg_backend_pid()"))
    with _REQUEST_TRACKER_LOCK:
        _REQUEST_PIDS[label] = pid
        connected = _REQUEST_CONNECTED.get(label)
    if connected is not None:
        connected.set()
    session.info["test_request_label"] = label


def _next_period() -> tuple[int, int]:
    global _PERIOD_INDEX
    year = 2000 + _PERIOD_INDEX // 12
    month = _PERIOD_INDEX % 12 + 1
    _PERIOD_INDEX += 1
    assert year <= 2100
    return year, month


def _add_schedule(
    factory: sessionmaker[Session], creator_id: int, *, status: str = "draft"
) -> int:
    year, month = _next_period()
    with factory() as session:
        schedule = Schedule(
            year=year,
            month=month,
            status=status,
            created_by=creator_id,
            collection_opened_at=(
                datetime(2026, 1, 1, tzinfo=UTC) if status != "draft" else None
            ),
            collection_closed_at=(
                datetime(2026, 1, 2, tzinfo=UTC)
                if status in {"collection_closed", "closed"}
                else None
            ),
        )
        session.add(schedule)
        session.commit()
        return schedule.id


def _headers(token: str, label: str | None = None) -> dict[str, str]:
    result = {"Authorization": f"Bearer {token}"}
    if label:
        result["X-Test-Request"] = label
    return result


def _post(
    app,
    token: str,
    path: str,
    *,
    label: str,
    payload: dict[str, int] | None = None,
):
    with TestClient(app) as client:
        return client.post(path, json=payload, headers=_headers(token, label))


def _delete(app, token: str, schedule_id: int, *, label: str):
    with TestClient(app) as client:
        return client.delete(f"{PATH}/{schedule_id}", headers=_headers(token, label))


def _wait_until_blocked(
    factory: sessionmaker[Session], blocked_pid: int, holder_pid: int
) -> None:
    """Wait until PostgreSQL confirms the expected lock wait, with a timeout."""
    engine = factory.kw["bind"]
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            blockers = connection.scalar(
                text("SELECT pg_blocking_pids(:pid)"), {"pid": blocked_pid}
            )
        if holder_pid in blockers:
            return
        time.sleep(0.02)
    pytest.fail(
        f"Backend {blocked_pid} did not wait for backend {holder_pid} as expected"
    )


def _held_http_pair(
    schedules_api,
    schedule_id: int,
    first: tuple[str, str, dict[str, int] | None],
    second: tuple[str, str, dict[str, int] | None],
) -> tuple[object, object]:
    """Hold the first successful request before commit while its peer blocks."""
    app, token, _, factory, *_ = schedules_api
    first_ready = Event()
    release_first = Event()
    second_connected = Event()
    _REQUEST_PIDS.clear()
    _REQUEST_CONNECTED.clear()
    _REQUEST_CONNECTED["second"] = second_connected

    def pause_first(session: Session) -> None:
        if session.info.get("test_request_label") == "first":
            first_ready.set()
            assert release_first.wait(timeout=12), (
                "Timed out waiting to release first request"
            )

    event.listen(Session, "before_commit", pause_first)

    def run(request: tuple[str, str, dict[str, int] | None], label: str):
        method, suffix, payload = request
        path = PATH if schedule_id == 0 else f"{PATH}/{schedule_id}{suffix}"
        with TestClient(app) as client:
            return client.request(
                method,
                path,
                json=payload,
                headers=_headers(token, label),
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            first_future = workers.submit(run, first, "first")
            assert first_ready.wait(timeout=10), "First request did not reach commit"
            with _REQUEST_TRACKER_LOCK:
                first_pid = _REQUEST_PIDS["first"]
            second_future = workers.submit(run, second, "second")
            assert second_connected.wait(timeout=8), "Second request did not connect"
            with _REQUEST_TRACKER_LOCK:
                second_pid = _REQUEST_PIDS["second"]
            _wait_until_blocked(factory, second_pid, first_pid)
            release_first.set()
            return first_future.result(timeout=16), second_future.result(timeout=16)
    finally:
        release_first.set()
        event.remove(Session, "before_commit", pause_first)
        _REQUEST_CONNECTED.clear()
        _REQUEST_PIDS.clear()


def _counts(factory: sessionmaker[Session], schedule_id: int) -> dict[str, int]:
    with factory() as session:
        return {
            "schedules": session.scalar(
                select(func.count())
                .select_from(Schedule)
                .where(Schedule.id == schedule_id)
            ),
            "requirements": session.scalar(
                select(func.count())
                .select_from(ShiftRequirement)
                .where(ShiftRequirement.schedule_id == schedule_id)
            ),
            "submissions": session.scalar(
                select(func.count())
                .select_from(AvailabilitySubmission)
                .where(AvailabilitySubmission.schedule_id == schedule_id)
            ),
        }


def _insert_child(
    factory: sessionmaker[Session],
    *,
    kind: str,
    schedule_id: int,
    position_id: int,
    employee_id: int,
    ready: Event,
    inserted: Event,
    release: Event,
    pid_out: dict[str, int],
) -> tuple[str, int | None]:
    """Insert and hold one direct child FK lock until the controller releases it."""
    with factory() as session:
        pid_out["pid"] = session.scalar(text("SELECT pg_backend_pid()"))
        ready.set()
        if kind == "shift_requirements":
            schedule = session.get(Schedule, schedule_id)
            assert schedule is not None
            row = ShiftRequirement(
                schedule_id=schedule_id,
                position_id=position_id,
                work_date=date(schedule.year, schedule.month, 15),
                start_time=local_time(9),
                end_time=local_time(17),
                required_workers=1,
            )
        elif kind == "availability_submissions":
            row = AvailabilitySubmission(schedule_id=schedule_id, user_id=employee_id)
        else:
            raise AssertionError(f"Unsupported direct child table: {kind}")
        session.add(row)
        try:
            session.flush()
            inserted.set()
            assert release.wait(timeout=12), (
                "Timed out waiting to release child transaction"
            )
            row_id = row.id
            session.commit()
            return "committed", row_id
        except IntegrityError as exc:
            sqlstate = getattr(exc.orig, "sqlstate", None)
            session.rollback()
            return f"integrity_error:{sqlstate}", None


def _race_child_insert_first(
    schedules_api, schedule_id: int, kind: str
) -> tuple[object, tuple[str, int | None]]:
    app, token, _, factory, _, employee_id, position_id = schedules_api
    ready, inserted, release = Event(), Event(), Event()
    pid_out: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=1) as workers:
        child_future = workers.submit(
            _insert_child,
            factory,
            kind=kind,
            schedule_id=schedule_id,
            position_id=position_id,
            employee_id=employee_id,
            ready=ready,
            inserted=inserted,
            release=release,
            pid_out=pid_out,
        )
        assert ready.wait(timeout=8)
        assert inserted.wait(timeout=8), (
            "Child INSERT did not acquire its parent FK lock"
        )
        label = f"delete-{kind}"
        _REQUEST_PIDS.clear()
        _REQUEST_CONNECTED.clear()
        connected = Event()
        _REQUEST_CONNECTED[label] = connected
        try:
            with ThreadPoolExecutor(max_workers=1) as requests:
                deletion = requests.submit(
                    _delete, app, token, schedule_id, label=label
                )
                assert connected.wait(timeout=8)
                with _REQUEST_TRACKER_LOCK:
                    delete_pid = _REQUEST_PIDS[label]
                _wait_until_blocked(factory, delete_pid, pid_out["pid"])
                release.set()
                response = deletion.result(timeout=16)
            return response, child_future.result(timeout=16)
        finally:
            release.set()
            _REQUEST_CONNECTED.clear()
            _REQUEST_PIDS.clear()


def _race_delete_first_against_child(schedules_api, schedule_id: int, kind: str):
    app, token, _, factory, _, employee_id, position_id = schedules_api
    ready, inserted, release_child = Event(), Event(), Event()
    release_delete = Event()
    child_pid: dict[str, int] = {}
    _REQUEST_PIDS.clear()
    _REQUEST_CONNECTED.clear()
    connected = Event()
    _REQUEST_CONNECTED["delete-first"] = connected

    def pause_delete(session: Session) -> None:
        if session.info.get("test_request_label") == "delete-first":
            release_delete.set()  # DELETE has acquired and used the parent row.
            assert continue_delete.wait(timeout=12), (
                "Timed out waiting to commit DELETE"
            )

    continue_delete = Event()
    event.listen(Session, "before_commit", pause_delete)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            deletion = workers.submit(
                _delete, app, token, schedule_id, label="delete-first"
            )
            assert release_delete.wait(timeout=10), (
                "DELETE did not reach its commit boundary"
            )
            with _REQUEST_TRACKER_LOCK:
                delete_pid = _REQUEST_PIDS["delete-first"]
            child = workers.submit(
                _insert_child,
                factory,
                kind=kind,
                schedule_id=schedule_id,
                position_id=position_id,
                employee_id=employee_id,
                ready=ready,
                inserted=inserted,
                release=release_child,
                pid_out=child_pid,
            )
            assert ready.wait(timeout=8)
            _wait_until_blocked(factory, child_pid["pid"], delete_pid)
            continue_delete.set()
            response = deletion.result(timeout=16)
            child_outcome = child.result(timeout=16)
            return response, child_outcome
    finally:
        continue_delete.set()
        release_child.set()
        event.remove(Session, "before_commit", pause_delete)
        _REQUEST_CONNECTED.clear()
        _REQUEST_PIDS.clear()


def test_postgres_create_and_unique_conflict_rollback(schedules_api) -> None:
    app, token, _, factory, manager_id, *_ = schedules_api
    year, month = _next_period()
    first, duplicate = _held_http_pair(
        schedules_api,
        schedule_id=0,
        first=("POST", "", {"year": year, "month": month}),
        second=("POST", "", {"year": year, "month": month}),
    )
    assert sorted([first.status_code, duplicate.status_code]) == [201, 409]
    conflict = first if first.status_code == 409 else duplicate
    assert (
        conflict.json()["type"] == "urn:dreamplanner:problem:schedule-period-conflict"
    )
    assert "uq_schedules_period" not in conflict.text
    with factory() as session:
        rows = session.scalars(
            select(Schedule).where(Schedule.year == year, Schedule.month == month)
        ).all()
        assert len(rows) == 1
        assert rows[0].status == "draft"
        assert rows[0].created_by == manager_id
        new_year, new_month = _next_period()
        created = create_schedule(session, new_year, new_month, manager_id)
        assert created.status == "draft"


def test_postgres_other_integrity_error_is_not_a_period_conflict(schedules_api) -> None:
    _, _, _, factory, manager_id, *_ = schedules_api
    year, month = _next_period()
    with factory() as session:
        with pytest.raises(IntegrityError) as error:
            create_schedule(session, year, month, manager_id + 10_000_000)
        assert getattr(error.value.orig, "sqlstate", None) == "23503"
        assert not isinstance(error.value, SchedulePeriodConflict)
        assert not session.in_transaction()
        following_year, following_month = _next_period()
        assert (
            create_schedule(session, following_year, following_month, manager_id).status
            == "draft"
        )


def test_two_parallel_deletes_return_204_then_404(schedules_api) -> None:
    schedule_id = _add_schedule(schedules_api[3], schedules_api[4])
    first, second = _held_http_pair(
        schedules_api,
        schedule_id,
        ("DELETE", "", None),
        ("DELETE", "", None),
    )
    assert sorted([first.status_code, second.status_code]) == [204, 404]
    assert _counts(schedules_api[3], schedule_id) == {
        "schedules": 0,
        "requirements": 0,
        "submissions": 0,
    }


@pytest.mark.parametrize("kind", ["shift_requirements", "availability_submissions"])
def test_child_insert_wins_before_delete_cascades(schedules_api, kind) -> None:
    schedule_id = _add_schedule(schedules_api[3], schedules_api[4])
    response, child_result = _race_child_insert_first(schedules_api, schedule_id, kind)
    assert child_result[0] == "committed"
    assert response.status_code == 204
    counts = _counts(schedules_api[3], schedule_id)
    assert counts == {"schedules": 0, "requirements": 0, "submissions": 0}


@pytest.mark.parametrize("kind", ["shift_requirements", "availability_submissions"])
def test_delete_wins_before_child_insert_fk_rejects_orphan(schedules_api, kind) -> None:
    schedule_id = _add_schedule(schedules_api[3], schedules_api[4])
    response, child_result = _race_delete_first_against_child(
        schedules_api, schedule_id, kind
    )
    assert response.status_code == 204
    assert child_result == ("integrity_error:23503", None)
    assert _counts(schedules_api[3], schedule_id) == {
        "schedules": 0,
        "requirements": 0,
        "submissions": 0,
    }


def test_delete_cascades_deep_children(schedules_api) -> None:
    app, token, _, factory, manager_id, employee_id, position_id = schedules_api
    schedule_id = _add_schedule(factory, manager_id)
    with factory() as session:
        schedule = session.get(Schedule, schedule_id)
        requirement = ShiftRequirement(
            schedule_id=schedule_id,
            position_id=position_id,
            work_date=date(schedule.year, schedule.month, 15),
            start_time=local_time(9),
            end_time=local_time(17),
            required_workers=1,
        )
        submission = AvailabilitySubmission(
            schedule_id=schedule_id, user_id=employee_id
        )
        session.add_all([requirement, submission])
        session.flush()
        session.add_all(
            [
                ShiftAssignment(
                    shift_requirement_id=requirement.id,
                    user_id=employee_id,
                    assigned_by=manager_id,
                ),
                AvailabilityEntry(
                    submission_id=submission.id,
                    work_date=date(schedule.year, schedule.month, 15),
                    start_time=local_time(9),
                    end_time=local_time(10),
                    status="available",
                ),
            ]
        )
        requirement_id, submission_id = requirement.id, submission.id
        session.commit()
    with TestClient(app) as client:
        response = client.delete(f"{PATH}/{schedule_id}", headers=_headers(token))
    assert response.status_code == 204
    with factory() as session:
        assert session.get(Schedule, schedule_id) is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(ShiftRequirement)
                .where(ShiftRequirement.schedule_id == schedule_id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AvailabilitySubmission)
                .where(AvailabilitySubmission.schedule_id == schedule_id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(ShiftAssignment)
                .where(ShiftAssignment.shift_requirement_id == requirement_id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AvailabilityEntry)
                .where(AvailabilityEntry.submission_id == submission_id)
            )
            == 0
        )


def test_delete_then_collection_opening_returns_404(schedules_api) -> None:
    schedule_id = _add_schedule(schedules_api[3], schedules_api[4])
    deleted, opened = _held_http_pair(
        schedules_api,
        schedule_id,
        ("DELETE", "", None),
        ("POST", "/collection-opening", None),
    )
    assert deleted.status_code == 204
    assert opened.status_code == 404
    assert _counts(schedules_api[3], schedule_id)["schedules"] == 0


def test_collection_opening_then_delete_returns_204(schedules_api) -> None:
    schedule_id = _add_schedule(schedules_api[3], schedules_api[4])
    opened, deleted = _held_http_pair(
        schedules_api,
        schedule_id,
        ("POST", "/collection-opening", None),
        ("DELETE", "", None),
    )
    assert opened.status_code == 200
    assert deleted.status_code == 204
    with schedules_api[3]() as session:
        schedule = session.get(Schedule, schedule_id)
        assert schedule is None


@pytest.mark.parametrize(
    ("status", "suffix", "timestamp_field", "expected_status"),
    [
        ("draft", "/collection-opening", "collection_opened_at", "collecting"),
        (
            "collecting",
            "/collection-closing",
            "collection_closed_at",
            "collection_closed",
        ),
        ("collection_closed", "/closing", None, "closed"),
    ],
)
def test_two_parallel_same_stage_transitions_are_serialized(
    schedules_api, status, suffix, timestamp_field, expected_status
) -> None:
    factory, manager_id = schedules_api[3], schedules_api[4]
    schedule_id = _add_schedule(factory, manager_id, status=status)
    first, second = _held_http_pair(
        schedules_api,
        schedule_id,
        ("POST", suffix, None),
        ("POST", suffix, None),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    with factory() as session:
        schedule = session.get(Schedule, schedule_id)
        assert schedule is not None
        assert schedule.status == expected_status
        if timestamp_field is None:
            assert schedule.collection_opened_at == datetime(2026, 1, 1, tzinfo=UTC)
            assert schedule.collection_closed_at == datetime(2026, 1, 2, tzinfo=UTC)
        else:
            assert getattr(schedule, timestamp_field) is not None
            assert getattr(schedule, timestamp_field).tzinfo is not None
        assert schedule.published_at is None


def test_distinct_transitions_are_serialized_in_order(schedules_api) -> None:
    factory, manager_id = schedules_api[3], schedules_api[4]
    schedule_id = _add_schedule(factory, manager_id)
    opened, collection_closed = _held_http_pair(
        schedules_api,
        schedule_id,
        ("POST", "/collection-opening", None),
        ("POST", "/collection-closing", None),
    )
    assert opened.status_code == collection_closed.status_code == 200
    with factory() as session:
        schedule = session.get(Schedule, schedule_id)
        assert schedule is not None
        assert schedule.status == "collection_closed"
        assert schedule.collection_opened_at is not None
        assert schedule.collection_closed_at is not None
        assert schedule.collection_opened_at < schedule.collection_closed_at
        timestamps = (schedule.collection_opened_at, schedule.collection_closed_at)

    closed, repeated = _held_http_pair(
        schedules_api,
        schedule_id,
        ("POST", "/closing", None),
        ("POST", "/closing", None),
    )
    assert sorted([closed.status_code, repeated.status_code]) == [200, 409]
    with factory() as session:
        schedule = session.get(Schedule, schedule_id)
        assert schedule is not None and schedule.status == "closed"
        assert (
            schedule.collection_opened_at,
            schedule.collection_closed_at,
        ) == timestamps
