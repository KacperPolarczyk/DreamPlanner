"""PostgreSQL constraints and ordered write races for shift assignments."""

import base64
import os
import secrets
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from datetime import time as local_time
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from src.assignments.models import ShiftAssignment
from src.assignments.repository import ShiftAssignmentRepository
from src.assignments.service import (
    AssignmentAlreadyExists,
    AssignmentCapacityExceeded,
    AssignmentNotFound,
    AssignmentOverlap,
    AssignmentScheduleStateConflict,
    _assignment_db_conflict,
    create_shift_assignment,
    delete_shift_assignment,
)
from src.auth import session_service
from src.availability.service import (
    AvailabilityScheduleStateConflict,
    create_availability_submission,
)
from src.core.config import Settings
from src.core.database import get_session
from src.main import create_app
from src.positions.models import Position
from src.schedules.models import Schedule, ShiftRequirement
from src.schedules.repository import ScheduleRepository
from src.schedules.service import (
    ScheduleStateConflict,
    delete_shift_requirement,
    update_shift_requirement,
)
from src.users.models import Role, User

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("TEST_DATABASE_URL")
        or not os.getenv("TEST_MIGRATION_DATABASE_URL"),
        reason="Set both TEST_*_DATABASE_URL values for the dedicated database",
    ),
]
DB_NAME = "dreamplanner_test_assignments_125"
_PERIOD = 0
_EXPECTED_CONFLICTS = (
    AssignmentAlreadyExists,
    AssignmentCapacityExceeded,
    AssignmentNotFound,
    AssignmentOverlap,
    AssignmentScheduleStateConflict,
    ScheduleStateConflict,
    AvailabilityScheduleStateConflict,
)
INSERT = (
    "INSERT INTO shift_assignments "
    "(shift_requirement_id,user_id,assigned_by,warning_type,warning_acknowledged_at) "
    "VALUES (:rid,:uid,:manager,:warning,:ack) RETURNING id"
)


@pytest.fixture(scope="module")
def postgres_factory() -> Iterator[sessionmaker[Session]]:
    """Migrate only the verified empty database dedicated to this module."""
    migration_url = os.environ["TEST_MIGRATION_DATABASE_URL"]
    app_url = os.environ["TEST_DATABASE_URL"]
    migration, app = make_url(migration_url), make_url(app_url)
    assert migration.drivername == app.drivername == "postgresql+psycopg"
    assert migration.host in {"127.0.0.1", "localhost"}
    assert (migration.host, migration.port, migration.database) == (
        app.host,
        app.port,
        app.database,
    )
    assert migration.database == DB_NAME
    assert migration.username == "dreamplanner_migration"
    assert app.username == "dreamplanner_app"
    assert DB_NAME not in {
        make_url(url).database
        for url in (Settings().database_url, Settings().migration_database_url)
        if url
    }
    migration_engine = create_engine(migration_url)
    app_engine = create_engine(
        app_url,
        connect_args={"options": "-c lock_timeout=8000 -c statement_timeout=20000"},
    )
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    upgraded = False
    try:
        with migration_engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
            assert tables <= {"alembic_version"}, (
                f"Test database is not empty: {tables}"
            )
            if "alembic_version" in tables:
                assert (
                    connection.scalar(text("SELECT count(*) FROM alembic_version")) == 0
                )
            assert connection.scalar(text("SELECT current_database()")) == DB_NAME
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname=current_database() AND pid<>pg_backend_pid()"
                    )
                )
                == 0
            )
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("MIGRATION_DATABASE_URL", migration_url)
            command.upgrade(config, "head")
            upgraded = True
            yield sessionmaker(bind=app_engine, autoflush=False)
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
def data(postgres_factory: sessionmaker[Session]) -> dict[str, object]:
    """Create a unique closed schedule, workers, and two test positions."""
    global _PERIOD
    _PERIOD += 1
    year, month = 2060 + _PERIOD // 12, _PERIOD % 12 + 1
    with postgres_factory() as session:
        manager_role = session.scalar(select(Role.id).where(Role.code == "manager"))
        employee_role = session.scalar(select(Role.id).where(Role.code == "employee"))
        manager = User(
            role_id=manager_role,
            email=f"assignment-manager-{uuid4()}@example.test",
            full_name="Assignment Test Manager",
            password_hash="unused",
            is_active=True,
            activated_at=datetime.now(UTC),
        )
        workers = [
            User(
                role_id=employee_role,
                email=f"assignment-worker-{uuid4()}@example.test",
                full_name="Assignment Test Worker",
                password_hash="unused",
                is_active=True,
                activated_at=datetime.now(UTC),
            )
            for _ in range(3)
        ]
        position = Position(name=f"Assignment Test Position {uuid4()}", is_active=True)
        schedule = Schedule(year=year, month=month, status="closed", creator=manager)
        session.add_all([manager, *workers, position, schedule])
        session.flush()
        result = {
            "schedule": schedule.id,
            "manager": manager.id,
            "workers": tuple(worker.id for worker in workers),
            "position": position.id,
            "day": date(year, month, 5),
        }
        session.commit()
        return result


@pytest.fixture
def assignments_api(postgres_factory, data, monkeypatch):
    """Use real PostgreSQL request sessions with the normal Bearer dependency."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "assignment-postgres-v1")
    monkeypatch.setenv("JWT_ISSUER", "assignment-postgres")
    monkeypatch.setenv("JWT_AUDIENCE", "assignment-postgres-api")
    app = create_app()
    session_id = uuid4()
    actor = SimpleNamespace(
        id=data["manager"],
        is_active=True,
        activated_at=datetime.now(UTC),
        role=SimpleNamespace(code="manager"),
    )
    auth_session = SimpleNamespace(
        user_id=data["manager"],
        revoked_at=None,
        idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
        absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
    )

    class AuthRepository:
        def get_session(self, requested_id):
            return auth_session if requested_id == session_id else None

        def get_user_fresh(self, requested_id):
            return actor if requested_id == data["manager"] else None

    monkeypatch.setattr(
        session_service, "AuthSessionRepository", lambda session: AuthRepository()
    )

    def test_session():
        with postgres_factory() as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    token = app.state.jwt_codec.issue(data["manager"], session_id)
    yield app, token
    app.dependency_overrides.clear()


def _requirement(factory, data, start=8, end=10, *, day=None, workers=2) -> int:
    with factory() as session:
        requirement = ShiftRequirement(
            schedule_id=data["schedule"],
            position_id=data["position"],
            work_date=day or data["day"],
            start_time=local_time(start),
            end_time=local_time(end),
            required_workers=workers,
        )
        session.add(requirement)
        session.commit()
        return requirement.id


def _params(data, rid, uid, *, warning=None, ack=None):
    return {
        "rid": rid,
        "uid": uid,
        "manager": data["manager"],
        "warning": warning,
        "ack": ack,
    }


def _raw_insert(session, data, rid, uid, *, warning=None, ack=None) -> int:
    return session.scalar(
        text(INSERT), _params(data, rid, uid, warning=warning, ack=ack)
    )


def _rejected(session, statement, params, state, constraint=None) -> DBAPIError:
    with pytest.raises(DBAPIError) as error:
        with session.begin_nested():
            session.execute(text(statement), params)
    assert error.value.orig.sqlstate == state
    if constraint is not None:
        assert error.value.orig.diag.constraint_name == constraint
    assert session.scalar(text("SELECT 1")) == 1
    return error.value


def _count(factory, rid) -> int:
    with factory() as session:
        return session.scalar(
            select(func.count(ShiftAssignment.id)).where(
                ShiftAssignment.shift_requirement_id == rid
            )
        )


def _post(session, data, rid, uid):
    return create_shift_assignment(
        session,
        data["schedule"],
        actor_id=data["manager"],
        actor_role="manager",
        requirement_id=rid,
        user_id=uid,
        warning_acknowledged=True,
    )


def _delete(session, data, assignment_id):
    return delete_shift_assignment(
        session, data["schedule"], assignment_id, actor_role="manager"
    )


def _wait_blocked(factory, waiting_pid, blocking_pid) -> None:
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        with factory() as session:
            if blocking_pid in session.scalar(
                text("SELECT pg_blocking_pids(:pid)"), {"pid": waiting_pid}
            ):
                return
        time.sleep(0.02)
    pytest.fail("Second PostgreSQL connection did not wait for the schedule lock")


def _ordered(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    first: Callable[[Session], object],
    second: Callable[[Session], object],
) -> tuple[object, object]:
    """Hold the first real schedule lock and verify PostgreSQL blocks the second."""
    barrier = Barrier(2)
    first_locked, second_started, release = Event(), Event(), Event()
    pids: dict[str, int] = {}
    original = ScheduleRepository.get_by_id_for_update

    def hold_first(repository, schedule_id):
        schedule = original(repository, schedule_id)
        if repository.session.info.get("race_label") == "first":
            first_locked.set()
            assert release.wait(timeout=12)
        return schedule

    with monkeypatch.context() as patch:
        patch.setattr(ScheduleRepository, "get_by_id_for_update", hold_first)

        def run(operation, label):
            with factory() as session:
                session.info["race_label"] = label
                pids[label] = session.scalar(text("SELECT pg_backend_pid()"))
                barrier.wait(timeout=8)
                if label == "second":
                    assert first_locked.wait(timeout=8)
                    second_started.set()
                try:
                    return operation(session)
                except _EXPECTED_CONFLICTS as exc:
                    return type(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(run, first, "first")
            second_future = pool.submit(run, second, "second")
            try:
                assert first_locked.wait(timeout=8)
                assert second_started.wait(timeout=8)
                _wait_blocked(factory, pids["second"], pids["first"])
            finally:
                release.set()
            return first_future.result(timeout=20), second_future.result(timeout=20)


def _publish(session, data):
    schedule = ScheduleRepository(session).get_by_id_for_update(data["schedule"])
    assert schedule.status == "closed"
    schedule.status = "published"
    schedule.published_at = datetime.now(UTC)
    session.flush()
    session.commit()


def test_http_post_delete_and_problem_mapping(
    assignments_api, postgres_factory, data
) -> None:
    """Exercise the real HTTP mapping against committed PostgreSQL state."""
    app, token = assignments_api
    rid = _requirement(postgres_factory, data, workers=1)
    first_worker, second_worker = data["workers"][:2]
    collection = f"/api/v1/schedules/{data['schedule']}/shift-assignments"
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        created = client.post(
            collection,
            json={
                "shift_requirement_id": str(rid),
                "user_id": str(first_worker),
                "warning_acknowledged": True,
            },
            headers=headers,
        )
        assert created.status_code == 201
        detail = f"{collection}/{created.json()['id']}"
        full = client.post(
            collection,
            json={
                "shift_requirement_id": str(rid),
                "user_id": str(second_worker),
                "warning_acknowledged": True,
            },
            headers=headers,
        )
        removed = client.delete(detail, headers=headers)
        repeated = client.delete(detail, headers=headers)
        after = client.post(
            collection,
            json={
                "shift_requirement_id": str(rid),
                "user_id": str(second_worker),
                "warning_acknowledged": True,
            },
            headers=headers,
        )
    assert full.status_code == 409
    assert (
        full.json()["type"] == "urn:dreamplanner:problem:assignment-capacity-exceeded"
    )
    assert full.headers["content-type"] == "application/problem+json"
    assert removed.status_code == 204 and removed.content == b""
    assert repeated.status_code == 404
    assert after.status_code == 201
    assert _count(postgres_factory, rid) == 1


def test_schema_columns_constraints_indexes_and_trigger(postgres_factory) -> None:
    with postgres_factory() as session:
        rows = session.execute(
            text(
                "SELECT column_name,data_type,is_nullable,is_identity "
                "FROM information_schema.columns WHERE table_name='shift_assignments'"
            )
        ).all()
        columns = {row.column_name: row for row in rows}
        assert set(columns) == {
            "id",
            "shift_requirement_id",
            "user_id",
            "assigned_by",
            "assigned_at",
            "warning_type",
            "warning_acknowledged_at",
        }
        for name in ("id", "shift_requirement_id", "user_id", "assigned_by"):
            assert columns[name].data_type == "bigint"
            assert columns[name].is_nullable == "NO"
        assert columns["id"].is_identity == "YES"
        assert columns["assigned_at"].data_type == "timestamp with time zone"
        assert columns["assigned_at"].is_nullable == "NO"
        assert columns["warning_type"].data_type == "character varying"
        assert columns["warning_type"].is_nullable == "YES"
        assert (
            columns["warning_acknowledged_at"].data_type == "timestamp with time zone"
        )
        assert columns["warning_acknowledged_at"].is_nullable == "YES"
        definitions = dict(
            session.execute(
                text(
                    "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid='shift_assignments'::regclass"
                )
            ).all()
        )
        assert (
            "UNIQUE (shift_requirement_id, user_id)"
            in definitions["uq_shift_assignment"]
        )
        assert "warning_type" in definitions["chk_warning_ack_pair"]
        assert "warning_acknowledged_at" in definitions["chk_warning_ack_pair"]
        for column, target in (
            ("shift_requirement_id", "shift_requirements"),
            ("user_id", "users"),
            ("assigned_by", "users"),
        ):
            assert any(
                f"FOREIGN KEY ({column}) REFERENCES {target}(id)" in definition
                for definition in definitions.values()
            )
        assert any(
            "FOREIGN KEY (shift_requirement_id)" in definition
            and "ON DELETE CASCADE" in definition
            for definition in definitions.values()
        )
        assert all(
            "ON DELETE CASCADE" not in definition
            for definition in definitions.values()
            if "REFERENCES users(id)" in definition
        )
        indexes = set(
            session.scalars(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename='shift_assignments'"
                )
            )
        )
        assert "ix_shift_assignments_user" in indexes
        triggers = set(
            session.scalars(
                text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgrelid='shift_assignments'::regclass "
                    "AND NOT tgisinternal"
                )
            )
        )
        assert "trg_assignment_overlap" in triggers


def test_warning_checks_insert_and_update(postgres_factory, data) -> None:
    rid = _requirement(postgres_factory, data)
    other_rid = _requirement(postgres_factory, data, 12, 14)
    first, second, third = data["workers"]
    now = datetime.now(UTC)
    with postgres_factory() as session:
        plain = _raw_insert(session, data, rid, first)
        _raw_insert(session, data, rid, second, warning="unavailable", ack=now)
        _raw_insert(session, data, rid, third, warning="unknown", ack=now)
        for warning, ack, constraint in (
            ("unknown", None, "chk_warning_ack_pair"),
            (None, now, "chk_warning_ack_pair"),
            ("invalid", now, None),
        ):
            # Avoid masking CHECK behind UNIQUE or the overlap trigger.
            error = _rejected(
                session,
                INSERT,
                _params(data, other_rid, first, warning=warning, ack=ack),
                "23514",
                constraint,
            )
            assert _assignment_db_conflict(error) is None
        for statement, params, constraint in (
            (
                "UPDATE shift_assignments SET warning_type='unknown' WHERE id=:id",
                {"id": plain},
                "chk_warning_ack_pair",
            ),
            (
                "UPDATE shift_assignments SET warning_acknowledged_at=:ack "
                "WHERE id=:id",
                {"id": plain, "ack": now},
                "chk_warning_ack_pair",
            ),
            (
                "UPDATE shift_assignments SET warning_type='invalid', "
                "warning_acknowledged_at=:ack WHERE id=:id",
                {"id": plain, "ack": now},
                None,
            ),
        ):
            assert _rejected(session, statement, params, "23514", constraint)
        session.commit()


def test_foreign_keys_cascade_and_user_restrict(postgres_factory, data) -> None:
    rid = _requirement(postgres_factory, data)
    worker = data["workers"][0]
    with postgres_factory() as session:
        for column in ("shift_requirement_id", "user_id", "assigned_by"):
            params = _params(data, rid, worker)
            params[
                {
                    "shift_requirement_id": "rid",
                    "user_id": "uid",
                    "assigned_by": "manager",
                }[column]
            ] = 9223372036854775807
            error = _rejected(session, INSERT, params, "23503")
            assert _assignment_db_conflict(error) is None
        assignment_id = _raw_insert(session, data, rid, worker)
        error = _rejected(
            session,
            "UPDATE shift_assignments SET user_id=:uid WHERE id=:id",
            {"uid": 9223372036854775807, "id": assignment_id},
            "23503",
        )
        assert _assignment_db_conflict(error) is None
        assert _rejected(
            session, "DELETE FROM users WHERE id=:uid", {"uid": worker}, "23503"
        )
        session.execute(
            text("DELETE FROM shift_requirements WHERE id=:rid"), {"rid": rid}
        )
        assert (
            session.scalar(
                select(ShiftAssignment.id).where(ShiftAssignment.id == assignment_id)
            )
            is None
        )
        assert session.get(User, worker) is not None
        session.commit()


def test_overlap_trigger_insert_and_update(postgres_factory, data) -> None:
    first, second, _ = data["workers"]
    morning = _requirement(postgres_factory, data, 8, 10)
    touching = _requirement(postgres_factory, data, 10, 12)
    separate = _requirement(postgres_factory, data, 14, 16)
    partial = _requirement(postgres_factory, data, 9, 11)
    containing = _requirement(postgres_factory, data, 7, 13)
    next_day = _requirement(
        postgres_factory, data, 8, 10, day=data["day"].replace(day=6)
    )
    with postgres_factory() as session:
        _raw_insert(session, data, morning, first)
        _raw_insert(session, data, touching, first)
        _raw_insert(session, data, separate, first)
        _raw_insert(session, data, morning, second)
        _raw_insert(session, data, next_day, first)
        for rid in (partial, containing, morning):
            error = _rejected(session, INSERT, _params(data, rid, first), "P0001")
            assert (
                error.orig.diag.message_primary == "Pracownik ma kolidujacy przydzial"
            )
            assert _assignment_db_conflict(error) is AssignmentOverlap
        candidate = _raw_insert(session, data, partial, data["workers"][2])
        movable = _raw_insert(session, data, separate, data["workers"][2])
        for statement, params in (
            (
                "UPDATE shift_assignments SET user_id=:uid WHERE id=:id",
                {"uid": first, "id": candidate},
            ),
            (
                "UPDATE shift_assignments SET shift_requirement_id=:rid WHERE id=:id",
                {"rid": morning, "id": movable},
            ),
        ):
            error = _rejected(session, statement, params, "P0001")
            assert _assignment_db_conflict(error) is AssignmentOverlap
        session.commit()


def test_unique_constraint_with_trigger_disabled_in_rolled_back_transaction(
    postgres_factory, data
) -> None:
    """Expose UNIQUE; the earlier trigger normally catches the duplicate."""
    rid = _requirement(postgres_factory, data)
    worker = data["workers"][0]
    migration_engine = create_engine(os.environ["TEST_MIGRATION_DATABASE_URL"])
    try:
        with migration_engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(
                    text(
                        "ALTER TABLE shift_assignments "
                        "DISABLE TRIGGER trg_assignment_overlap"
                    )
                )
                connection.execute(text(INSERT), _params(data, rid, worker))
                error = _rejected(
                    connection,
                    INSERT,
                    _params(data, rid, worker),
                    "23505",
                    "uq_shift_assignment",
                )
                assert _assignment_db_conflict(error) is AssignmentAlreadyExists
            finally:
                transaction.rollback()
    finally:
        migration_engine.dispose()
    assert _count(postgres_factory, rid) == 0


@pytest.mark.parametrize("first_worker", [0, 1])
def test_concurrent_posts_respect_last_place(
    postgres_factory, data, monkeypatch, first_worker
) -> None:
    rid = _requirement(postgres_factory, data, workers=1)
    workers = data["workers"]
    first, second = _ordered(
        postgres_factory,
        monkeypatch,
        lambda session: _post(session, data, rid, workers[first_worker]),
        lambda session: _post(session, data, rid, workers[1 - first_worker]),
    )
    assert first.id
    assert second is AssignmentCapacityExceeded
    assert _count(postgres_factory, rid) == 1


def test_concurrent_duplicate_posts(postgres_factory, data, monkeypatch) -> None:
    rid = _requirement(postgres_factory, data)
    worker = data["workers"][0]
    first, second = _ordered(
        postgres_factory,
        monkeypatch,
        lambda session: _post(session, data, rid, worker),
        lambda session: _post(session, data, rid, worker),
    )
    assert first.id
    assert second is AssignmentAlreadyExists
    assert _count(postgres_factory, rid) == 1


def test_requirement_update_lock_blocks_post_key_share(postgres_factory, data) -> None:
    """Probe the real row-lock conflict without bypassing API status rules."""
    rid = _requirement(postgres_factory, data)
    worker = data["workers"][0]
    barrier = Barrier(2)
    locked, started, release = Event(), Event(), Event()
    pids = {}

    def holder():
        with postgres_factory() as session:
            pids["holder"] = session.scalar(text("SELECT pg_backend_pid()"))
            barrier.wait(timeout=8)
            session.scalar(
                select(ShiftRequirement.id)
                .where(ShiftRequirement.id == rid)
                .with_for_update()
            )
            locked.set()
            assert release.wait(timeout=12)
            session.commit()

    def assign():
        with postgres_factory() as session:
            pids["assign"] = session.scalar(text("SELECT pg_backend_pid()"))
            barrier.wait(timeout=8)
            assert locked.wait(timeout=8)
            started.set()
            return _post(session, data, rid, worker)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(holder)
        second = pool.submit(assign)
        try:
            assert locked.wait(timeout=8)
            assert started.wait(timeout=8)
            _wait_blocked(postgres_factory, pids["assign"], pids["holder"])
        finally:
            release.set()
        assert first.result(timeout=20) is None
        assert second.result(timeout=20).id
    assert _count(postgres_factory, rid) == 1


def test_post_key_share_blocks_requirement_update_lock(
    postgres_factory, data, monkeypatch
) -> None:
    rid = _requirement(postgres_factory, data)
    worker = data["workers"][0]
    barrier = Barrier(2)
    locked, started, release = Event(), Event(), Event()
    pids = {}
    original = ShiftAssignmentRepository.get_requirement_for_key_share

    def hold_key_share(repository, schedule_id, requirement_id):
        requirement = original(repository, schedule_id, requirement_id)
        locked.set()
        assert release.wait(timeout=12)
        return requirement

    def assign():
        with postgres_factory() as session:
            pids["assign"] = session.scalar(text("SELECT pg_backend_pid()"))
            barrier.wait(timeout=8)
            return _post(session, data, rid, worker)

    def holder():
        with postgres_factory() as session:
            pids["holder"] = session.scalar(text("SELECT pg_backend_pid()"))
            barrier.wait(timeout=8)
            assert locked.wait(timeout=8)
            started.set()
            result = session.scalar(
                select(ShiftRequirement.id)
                .where(ShiftRequirement.id == rid)
                .with_for_update()
            )
            session.commit()
            return result

    with monkeypatch.context() as patch:
        patch.setattr(
            ShiftAssignmentRepository, "get_requirement_for_key_share", hold_key_share
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(assign)
            second = pool.submit(holder)
            try:
                assert locked.wait(timeout=8)
                assert started.wait(timeout=8)
                _wait_blocked(postgres_factory, pids["holder"], pids["assign"])
            finally:
                release.set()
            assert first.result(timeout=20).id
            assert second.result(timeout=20) == rid
    assert _count(postgres_factory, rid) == 1


@pytest.mark.parametrize("first_requirement", [0, 1])
def test_concurrent_overlapping_posts(
    postgres_factory, data, monkeypatch, first_requirement
) -> None:
    requirements = (
        _requirement(postgres_factory, data, 8, 10),
        _requirement(postgres_factory, data, 9, 11),
    )
    worker = data["workers"][0]
    first, second = _ordered(
        postgres_factory,
        monkeypatch,
        lambda session: _post(session, data, requirements[first_requirement], worker),
        lambda session: _post(
            session, data, requirements[1 - first_requirement], worker
        ),
    )
    assert first.id
    assert second is AssignmentOverlap
    assert sum(_count(postgres_factory, rid) for rid in requirements) == 1


@pytest.mark.parametrize("first_requirement", [0, 1])
def test_concurrent_touching_posts(
    postgres_factory, data, monkeypatch, first_requirement
) -> None:
    requirements = (
        _requirement(postgres_factory, data, 8, 10),
        _requirement(postgres_factory, data, 10, 12),
    )
    worker = data["workers"][0]
    first, second = _ordered(
        postgres_factory,
        monkeypatch,
        lambda session: _post(session, data, requirements[first_requirement], worker),
        lambda session: _post(
            session, data, requirements[1 - first_requirement], worker
        ),
    )
    assert first.id and second.id
    assert sum(_count(postgres_factory, rid) for rid in requirements) == 2


@pytest.mark.parametrize("first_operation", ["delete", "post"])
def test_post_vs_delete(postgres_factory, data, monkeypatch, first_operation) -> None:
    rid = _requirement(postgres_factory, data, workers=1)
    old, new = data["workers"][:2]
    with postgres_factory() as session:
        assignment_id = _raw_insert(session, data, rid, old)
        session.commit()

    def delete(session):
        return _delete(session, data, assignment_id)

    def post(session):
        return _post(session, data, rid, new)

    first, second = _ordered(
        postgres_factory,
        monkeypatch,
        delete if first_operation == "delete" else post,
        post if first_operation == "delete" else delete,
    )
    if first_operation == "delete":
        assert first is None and second.id
        assert _count(postgres_factory, rid) == 1
    else:
        assert first is AssignmentCapacityExceeded and second is None
        assert _count(postgres_factory, rid) == 0


@pytest.mark.parametrize("first_label", ["one", "two"])
def test_two_deletes(postgres_factory, data, monkeypatch, first_label) -> None:
    rid = _requirement(postgres_factory, data)
    with postgres_factory() as session:
        removed = _raw_insert(session, data, rid, data["workers"][0])
        remaining = _raw_insert(session, data, rid, data["workers"][1])
        session.commit()
    operations = {
        "one": lambda session: _delete(session, data, removed),
        "two": lambda session: _delete(session, data, removed),
    }
    first, second = _ordered(
        postgres_factory,
        monkeypatch,
        operations[first_label],
        operations["two" if first_label == "one" else "one"],
    )
    assert first is None and second is AssignmentNotFound
    with postgres_factory() as session:
        assert session.get(ShiftAssignment, removed) is None
        assert session.get(ShiftAssignment, remaining) is not None


@pytest.mark.parametrize("first_operation", ["post", "publish"])
@pytest.mark.parametrize("write", ["post", "delete"])
def test_assignment_write_vs_publication(
    postgres_factory, data, monkeypatch, first_operation, write
) -> None:
    rid = _requirement(postgres_factory, data)
    worker = data["workers"][0]
    assignment_id = None
    if write == "delete":
        with postgres_factory() as session:
            assignment_id = _raw_insert(session, data, rid, worker)
            session.commit()
    operation = (
        (lambda session: _post(session, data, rid, worker))
        if write == "post"
        else (lambda session: _delete(session, data, assignment_id))
    )
    first, second = _ordered(
        postgres_factory,
        monkeypatch,
        operation
        if first_operation == "post"
        else lambda session: _publish(session, data),
        lambda session: (
            _publish(session, data) if first_operation == "post" else operation(session)
        ),
    )
    if first_operation == "post":
        assert first.id if write == "post" else first is None
        assert second is None
    else:
        assert first is None
        assert second is AssignmentScheduleStateConflict
    assert _count(postgres_factory, rid) == (
        1 if (write == "post") == (first_operation == "post") else 0
    )
    with postgres_factory() as session:
        assert session.get(Schedule, data["schedule"]).status == "published"


@pytest.mark.parametrize("operation", ["patch", "delete"])
@pytest.mark.parametrize("first_operation", ["post", "requirement"])
def test_requirement_write_is_forbidden_in_closed_even_when_racing_post(
    postgres_factory, data, monkeypatch, operation, first_operation
) -> None:
    rid = _requirement(postgres_factory, data)
    worker = data["workers"][0]
    requirement_write = (
        (
            lambda session: update_shift_requirement(
                session, data["schedule"], rid, changes={"required_workers": 3}
            )
        )
        if operation == "patch"
        else (lambda session: delete_shift_requirement(session, data["schedule"], rid))
    )

    def post(session):
        return _post(session, data, rid, worker)

    first, second = _ordered(
        postgres_factory,
        monkeypatch,
        post if first_operation == "post" else requirement_write,
        requirement_write if first_operation == "post" else post,
    )
    assert (
        first is ScheduleStateConflict if first_operation == "requirement" else first.id
    )
    assert second is ScheduleStateConflict if first_operation == "post" else second.id
    assert _count(postgres_factory, rid) == 1
    with postgres_factory() as session:
        assert session.get(ShiftRequirement, rid).required_workers == 2


def test_availability_write_is_forbidden_in_closed(postgres_factory, data) -> None:
    with postgres_factory() as session:
        with pytest.raises(AvailabilityScheduleStateConflict):
            create_availability_submission(
                session, data["schedule"], data["workers"][0], entries=[], submit=False
            )
    rid = _requirement(postgres_factory, data)
    with postgres_factory() as session:
        assert _post(session, data, rid, data["workers"][0]).id


def test_flush_integrity_rollback_and_clean_retry(
    postgres_factory, data, monkeypatch
) -> None:
    rid = _requirement(postgres_factory, data)
    worker = data["workers"][0]
    original = ShiftAssignmentRepository.add

    def invalid_warning(repository, assignment):
        assignment.warning_type = "unknown"
        assignment.warning_acknowledged_at = None
        original(repository, assignment)

    with postgres_factory() as session:
        with monkeypatch.context() as patch:
            patch.setattr(ShiftAssignmentRepository, "add", invalid_warning)
            with pytest.raises(DBAPIError) as error:
                _post(session, data, rid, worker)
            assert error.value.orig.sqlstate == "23514"
            assert error.value.orig.diag.constraint_name == "chk_warning_ack_pair"
            assert _assignment_db_conflict(error.value) is None
        assert session.scalar(text("SELECT 1")) == 1
        assert _post(session, data, rid, worker).id
    assert _count(postgres_factory, rid) == 1
    with postgres_factory() as session:
        assignment = session.scalar(
            select(ShiftAssignment).where(ShiftAssignment.shift_requirement_id == rid)
        )
        assert assignment.warning_type == "unknown"
        assert assignment.warning_acknowledged_at is not None
        assert session.get(ShiftRequirement, rid).required_workers == 2
        assert session.get(User, worker).is_active


def test_month_scope_unique_and_direct_writer_gap(postgres_factory, data) -> None:
    rid = _requirement(postgres_factory, data)
    with postgres_factory() as session:
        duplicate = Schedule(
            year=data["day"].year,
            month=data["day"].month,
            status="closed",
            created_by=data["manager"],
        )
        session.add(duplicate)
        with pytest.raises(DBAPIError) as error:
            session.flush()
        assert error.value.orig.sqlstate == "23505"
        assert error.value.orig.diag.constraint_name == "uq_schedules_period"
        session.rollback()
        outside = data["day"].replace(month=2 if data["day"].month != 2 else 3)
        error = _rejected(
            session,
            "INSERT INTO shift_requirements "
            "(schedule_id,position_id,work_date,start_time,end_time,required_workers) "
            "VALUES (:sid,:pid,:day,'12:00','14:00',1)",
            {"sid": data["schedule"], "pid": data["position"], "day": outside},
            "P0001",
        )
        assert error.orig.diag.message_primary.startswith("Data ")
        # Direct SQL can change the schedule period after requirements exist.
        session.execute(
            text("UPDATE schedules SET year=year+1 WHERE id=:sid"),
            {"sid": data["schedule"]},
        )
        assert session.get(ShiftRequirement, rid).work_date.year != session.scalar(
            text("SELECT year FROM schedules WHERE id=:sid"), {"sid": data["schedule"]}
        )
        session.rollback()
