"""PostgreSQL integrity and ordered write races for shift requirements."""

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
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from src.assignments.models import ShiftAssignment
from src.auth import session_service
from src.core.config import Settings
from src.core.database import get_session
from src.main import create_app
from src.positions.models import Position
from src.positions.service import set_position_active
from src.schedules.models import Schedule, ShiftRequirement
from src.schedules.service import (
    PositionInactive,
    ScheduleStateConflict,
    ShiftRequirementAssigned,
    ShiftRequirementConflict,
    create_shift_requirement,
    delete_shift_requirement,
    open_collection,
    update_shift_requirement,
)
from src.users.models import Role, User

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("TEST_DATABASE_URL")
        or not os.getenv("TEST_MIGRATION_DATABASE_URL"),
        reason="Both TEST_*_DATABASE_URL values must identify a test database",
    ),
]

DB_NAME = "dreamplanner_test_shift_requirements_107"
_PERIOD = 0


@pytest.fixture(scope="module")
def postgres_factory() -> Iterator[sessionmaker[Session]]:
    """Migrate only the verified, empty database dedicated to this test module."""
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
                        "WHERE datname = current_database() AND pid <> pg_backend_pid()"
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
def data(postgres_factory: sessionmaker[Session]) -> tuple[int, int, int]:
    """Create a unique monthly schedule, manager, and active position."""
    global _PERIOD
    _PERIOD += 1
    with postgres_factory() as session:
        role_id = session.scalar(select(Role.id).where(Role.code == "manager"))
        user = User(
            role_id=role_id,
            email=f"shift-pg-{uuid4()}@example.test",
            full_name="Shift PostgreSQL Manager",
            password_hash="unused",
            is_active=True,
            activated_at=datetime.now(UTC),
        )
        position = Position(name=f"shift-pg-{uuid4()}", is_active=True)
        schedule = Schedule(
            year=2040 + _PERIOD // 12, month=_PERIOD % 12 + 1, creator=user
        )
        session.add_all([user, position, schedule])
        session.commit()
        return schedule.id, position.id, user.id


def _day(factory: sessionmaker[Session], schedule_id: int) -> date:
    with factory() as session:
        schedule = session.get(Schedule, schedule_id)
        return date(schedule.year, schedule.month, 5)


def _create(
    factory: sessionmaker[Session], schedule_id: int, position_id: int, **changes
) -> int:
    values = {
        "position_id": position_id,
        "work_date": _day(factory, schedule_id),
        "start_time": local_time(8),
        "end_time": local_time(16),
        "required_workers": 2,
        "note": None,
    } | changes
    with factory() as session:
        requirement, _ = create_shift_requirement(session, schedule_id, **values)
        return requirement.id


def _count(factory: sessionmaker[Session], model, **filters) -> int:
    with factory() as session:
        return session.scalar(
            select(func.count()).select_from(model).filter_by(**filters)
        )


def _assignment(session: Session, requirement_id: int, user_id: int) -> None:
    """Simulate future assignment creation after locking its requirement.

    Future assignment service must use the documented FOR KEY SHARE protocol before
    inspecting the requirement and inserting. The current API has no such service.
    """
    requirement = session.scalar(
        select(ShiftRequirement)
        .where(ShiftRequirement.id == requirement_id)
        .with_for_update(read=True, key_share=True)
    )
    if requirement is None:
        raise LookupError("requirement was deleted")
    session.add(
        ShiftAssignment(
            shift_requirement_id=requirement_id, user_id=user_id, assigned_by=user_id
        )
    )
    session.flush()
    session.commit()


def _integrity(session: Session, statement, params: dict, sqlstate: str) -> str | None:
    """Roll back one rejected statement without aborting the parent transaction."""
    with pytest.raises(DBAPIError) as error:
        with session.begin_nested():
            session.execute(text(statement), params)
    assert error.value.orig.sqlstate == sqlstate
    return error.value.orig.diag.constraint_name


def _values(factory: sessionmaker[Session], schedule_id: int, position_id: int) -> dict:
    return {
        "sid": schedule_id,
        "pid": position_id,
        "day": _day(factory, schedule_id),
        "start": local_time(8),
        "end": local_time(16),
        "workers": 2,
    }


INSERT = (
    "INSERT INTO shift_requirements "
    "(schedule_id,position_id,work_date,start_time,end_time,required_workers) "
    "VALUES (:sid,:pid,:day,:start,:end,:workers)"
)


def test_database_checks_unique_and_allowed_intervals(postgres_factory, data) -> None:
    schedule_id, position_id, _ = data
    values = _values(postgres_factory, schedule_id, position_id)
    with postgres_factory() as session:
        assert _integrity(session, INSERT, values | {"workers": 0}, "23514") is not None
        assert (
            _integrity(session, INSERT, values | {"end": local_time(8)}, "23514")
            == "chk_shift_same_day"
        )
        session.execute(text(INSERT), values)
        for workers in (2, 3):
            assert (
                _integrity(session, INSERT, values | {"workers": workers}, "23505")
                == "uq_shift_requirement"
            )
        session.execute(text(INSERT), values | {"start": local_time(9)})
        session.execute(
            text(INSERT),
            values | {"start": local_time(16), "end": local_time(18)},
        )
        session.commit()
    assert _count(postgres_factory, ShiftRequirement, schedule_id=schedule_id) == 3


def test_database_foreign_keys_and_period_trigger(postgres_factory, data) -> None:
    schedule_id, position_id, user_id = data
    values = _values(postgres_factory, schedule_id, position_id)
    with postgres_factory() as session:
        # The BEFORE trigger rejects an absent schedule before FK enforcement.
        assert (
            _integrity(session, INSERT, values | {"sid": 9223372036854775807}, "P0001")
            is None
        )
        assert (
            session.scalar(
                text(
                    "SELECT count(*) FROM pg_constraint WHERE conrelid = "
                    "'shift_requirements'::regclass AND contype = 'f' "
                    "AND confrelid = 'schedules'::regclass"
                )
            )
            == 1
        )
        assert _integrity(
            session, INSERT, values | {"pid": 9223372036854775807}, "23503"
        )
        assert _integrity(
            session,
            "INSERT INTO shift_assignments (shift_requirement_id,user_id,assigned_by) "
            "VALUES (:rid,:uid,:uid)",
            {"rid": 9223372036854775807, "uid": user_id},
            "23503",
        )
        outside = date(2039, 1, 5)
        assert _integrity(session, INSERT, values | {"day": outside}, "P0001") is None
        result = session.execute(text(INSERT + " RETURNING id"), values)
        requirement_id = result.scalar_one()
        assert (
            _integrity(
                session,
                "UPDATE shift_requirements SET work_date=:day WHERE id=:rid",
                {"day": outside, "rid": requirement_id},
                "P0001",
            )
            is None
        )
        other = Schedule(year=2060, month=1, created_by=user_id)
        session.add(other)
        session.flush()
        assert (
            _integrity(
                session,
                "UPDATE shift_requirements SET schedule_id=:sid WHERE id=:rid",
                {"sid": other.id, "rid": requirement_id},
                "P0001",
            )
            is None
        )
        assert session.get(ShiftRequirement, requirement_id).schedule_id == schedule_id
        session.commit()


def _wait_for_block(factory: sessionmaker[Session], blocked: int, holder: int) -> None:
    """Inspect PostgreSQL's wait graph, bounded by the connection timeouts."""
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        with factory.kw["bind"].connect() as connection:
            if holder in connection.scalar(
                text("SELECT pg_blocking_pids(:pid)"), {"pid": blocked}
            ):
                return
        time.sleep(0.02)
    pytest.fail(f"Backend {blocked} did not block on {holder}")


def _ordered(
    factory: sessionmaker[Session],
    first: Callable[[Session], object],
    second: Callable[[Session], object],
) -> tuple[object, object]:
    """Pause the first commit; prove the second waits on its transaction."""
    ready, connected, release = Event(), Event(), Event()
    pids: dict[str, int] = {}

    def run(operation, label):
        with factory() as session:
            pids[label] = session.scalar(text("SELECT pg_backend_pid()"))
            if label == "second":
                connected.set()
            else:

                def pause(_session):
                    ready.set()
                    assert release.wait(timeout=12)

                event.listen(session, "before_commit", pause, once=True)
            try:
                return operation(session)
            except (
                ScheduleStateConflict,
                PositionInactive,
                ShiftRequirementAssigned,
                ShiftRequirementConflict,
                LookupError,
            ) as exc:
                return type(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(run, first, "first")
        try:
            assert ready.wait(timeout=8)
            second_future = pool.submit(run, second, "second")
            assert connected.wait(timeout=8)
            _wait_for_block(factory, pids["second"], pids["first"])
        finally:
            release.set()
        return first_future.result(timeout=20), second_future.result(timeout=20)


@pytest.fixture
def api(postgres_factory, data, monkeypatch):
    """Route requests through real PostgreSQL sessions and manager authentication."""
    schedule_id, position_id, user_id = data
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "shift-pg-test-v1")
    monkeypatch.setenv("JWT_ISSUER", "shift-pg-test")
    monkeypatch.setenv("JWT_AUDIENCE", "shift-pg-api")
    app = create_app()
    session_id = uuid4()
    actor = SimpleNamespace(
        id=user_id,
        is_active=True,
        activated_at=datetime.now(UTC),
        role=SimpleNamespace(code="manager"),
    )
    auth_session = SimpleNamespace(
        user_id=user_id,
        revoked_at=None,
        idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
        absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
    )

    class AuthRepository:
        def get_session(self, requested_id):
            return auth_session if requested_id == session_id else None

        def get_user_fresh(self, requested_id):
            return actor if requested_id == user_id else None

    monkeypatch.setattr(
        session_service, "AuthSessionRepository", lambda session: AuthRepository()
    )

    def test_session():
        with postgres_factory() as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    yield app, app.state.jwt_codec.issue(user_id, session_id), schedule_id, position_id
    app.dependency_overrides.clear()


def _race(left, right):
    barrier = Barrier(2)

    def run(call):
        barrier.wait(timeout=8)
        return call()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, left)
        second = pool.submit(run, right)
        return first.result(timeout=20), second.result(timeout=20)


def test_concurrent_post_one_201_one_409(api, postgres_factory) -> None:
    app, token, schedule_id, position_id = api
    path = f"/api/v1/schedules/{schedule_id}/shift-requirements"
    body = {
        "position_id": str(position_id),
        "work_date": _day(postgres_factory, schedule_id).isoformat(),
        "start_time": "08:00:00",
        "end_time": "16:00:00",
        "required_workers": 2,
    }

    def post():
        with TestClient(app) as client:
            return client.post(
                path, json=body, headers={"Authorization": f"Bearer {token}"}
            )

    responses = _race(post, post)
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert next(r for r in responses if r.status_code == 409).json()["type"] == (
        "urn:dreamplanner:problem:shift-requirement-conflict"
    )
    assert _count(postgres_factory, ShiftRequirement, schedule_id=schedule_id) == 1


def test_concurrent_patch_different_rows_unique_conflict(api, postgres_factory) -> None:
    app, token, schedule_id, position_id = api
    left_id = _create(postgres_factory, schedule_id, position_id)
    right_id = _create(
        postgres_factory,
        schedule_id,
        position_id,
        start_time=local_time(9),
        end_time=local_time(17),
    )
    path = f"/api/v1/schedules/{schedule_id}/shift-requirements"

    def patch(requirement_id):
        with TestClient(app) as client:
            return client.patch(
                f"{path}/{requirement_id}",
                json={"start_time": "10:00:00", "end_time": "18:00:00"},
                headers={"Authorization": f"Bearer {token}"},
            )

    responses = _race(lambda: patch(left_id), lambda: patch(right_id))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert next(r for r in responses if r.status_code == 409).json()["type"] == (
        "urn:dreamplanner:problem:shift-requirement-conflict"
    )
    with postgres_factory() as session:
        rows = session.scalars(
            select(ShiftRequirement).where(ShiftRequirement.id.in_([left_id, right_id]))
        ).all()
        assert len(rows) == 2
        assert sum(row.start_time == local_time(10) for row in rows) == 1


def test_same_row_patch_follows_lock_order(postgres_factory, data) -> None:
    schedule_id, position_id, _ = data
    requirement_id = _create(postgres_factory, schedule_id, position_id)
    first, second = _ordered(
        postgres_factory,
        lambda session: update_shift_requirement(
            session, schedule_id, requirement_id, changes={"note": "first"}
        ),
        lambda session: update_shift_requirement(
            session, schedule_id, requirement_id, changes={"note": "second"}
        ),
    )
    assert first[0].note == "first" and second[0].note == "second"
    with postgres_factory() as session:
        assert session.get(ShiftRequirement, requirement_id).note == "second"


@pytest.mark.parametrize("operation", ["post", "patch", "delete"])
@pytest.mark.parametrize("first", ["write", "transition"])
def test_write_versus_schedule_transition(
    postgres_factory, data, operation, first
) -> None:
    schedule_id, position_id, _ = data
    requirement_id = None
    if operation != "post":
        requirement_id = _create(postgres_factory, schedule_id, position_id)

    def write(session):
        if operation == "post":
            return create_shift_requirement(
                session,
                schedule_id,
                position_id=position_id,
                work_date=_day(postgres_factory, schedule_id),
                start_time=local_time(10),
                end_time=local_time(18),
                required_workers=1,
                note=None,
            )
        if operation == "patch":
            return update_shift_requirement(
                session, schedule_id, requirement_id, changes={"note": "updated"}
            )
        return delete_shift_requirement(session, schedule_id, requirement_id)

    def transition(session):
        return open_collection(session, schedule_id)

    result = _ordered(
        postgres_factory,
        write if first == "write" else transition,
        transition if first == "write" else write,
    )
    with postgres_factory() as session:
        assert session.get(Schedule, schedule_id).status == "collecting"
        if first == "transition":
            assert result[1] is ScheduleStateConflict
            assert _count(
                postgres_factory, ShiftRequirement, schedule_id=schedule_id
            ) == (0 if operation == "post" else 1)
        elif operation == "delete":
            assert session.get(ShiftRequirement, requirement_id) is None
        elif operation == "patch":
            assert session.get(ShiftRequirement, requirement_id).note == "updated"
        else:
            assert (
                _count(postgres_factory, ShiftRequirement, schedule_id=schedule_id) == 1
            )


@pytest.mark.parametrize("operation", ["post", "patch"])
@pytest.mark.parametrize("first", ["write", "deactivate"])
def test_write_versus_position_deactivation(
    postgres_factory, data, operation, first
) -> None:
    schedule_id, old_position_id, _ = data
    with postgres_factory() as session:
        new_position = Position(name=f"shift-target-{uuid4()}", is_active=True)
        session.add(new_position)
        session.commit()
        new_position_id = new_position.id
    requirement_id = None
    if operation == "patch":
        requirement_id = _create(postgres_factory, schedule_id, old_position_id)

    def write(session):
        if operation == "post":
            return create_shift_requirement(
                session,
                schedule_id,
                position_id=new_position_id,
                work_date=_day(postgres_factory, schedule_id),
                start_time=local_time(8),
                end_time=local_time(16),
                required_workers=1,
                note=None,
            )
        return update_shift_requirement(
            session,
            schedule_id,
            requirement_id,
            changes={"position_id": new_position_id},
        )

    def deactivate(session):
        return set_position_active(session, new_position_id, is_active=False)

    result = _ordered(
        postgres_factory,
        write if first == "write" else deactivate,
        deactivate if first == "write" else write,
    )
    with postgres_factory() as session:
        assert session.get(Position, new_position_id).is_active is False
        if first == "deactivate":
            assert result[1] is PositionInactive
        else:
            assert result[0] is not PositionInactive
        rows = session.scalars(
            select(ShiftRequirement).where(ShiftRequirement.schedule_id == schedule_id)
        ).all()
        assert sum(row.position_id == new_position_id for row in rows) == (
            1 if first == "write" else 0
        )


def test_delete_assigned_requirement_returns_409(api, postgres_factory, data) -> None:
    app, token, schedule_id, position_id = api
    _, _, user_id = data
    requirement_id = _create(postgres_factory, schedule_id, position_id)
    with postgres_factory() as session:
        _assignment(session, requirement_id, user_id)
    with TestClient(app) as client:
        response = client.delete(
            f"/api/v1/schedules/{schedule_id}/shift-requirements/{requirement_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 409
    assert (
        response.json()["type"] == "urn:dreamplanner:problem:shift-requirement-assigned"
    )
    assert _count(postgres_factory, ShiftRequirement, id=requirement_id) == 1
    assert (
        _count(postgres_factory, ShiftAssignment, shift_requirement_id=requirement_id)
        == 1
    )


@pytest.mark.parametrize("operation", ["delete", "patch"])
def test_assignment_first_blocks_structural_write(
    postgres_factory, data, operation
) -> None:
    schedule_id, position_id, user_id = data
    requirement_id = _create(postgres_factory, schedule_id, position_id)

    def write(session):
        if operation == "delete":
            return delete_shift_requirement(session, schedule_id, requirement_id)
        return update_shift_requirement(
            session, schedule_id, requirement_id, changes={"required_workers": 3}
        )

    first, second = _ordered(
        postgres_factory,
        lambda session: _assignment(session, requirement_id, user_id),
        write,
    )
    assert first is None and second is ShiftRequirementAssigned
    with postgres_factory() as session:
        assert session.get(ShiftRequirement, requirement_id).required_workers == 2
    assert _count(postgres_factory, ShiftRequirement, id=requirement_id) == 1
    assert (
        _count(postgres_factory, ShiftAssignment, shift_requirement_id=requirement_id)
        == 1
    )


def test_delete_first_prevents_orphan_assignment(postgres_factory, data) -> None:
    schedule_id, position_id, user_id = data
    requirement_id = _create(postgres_factory, schedule_id, position_id)
    first, second = _ordered(
        postgres_factory,
        lambda session: delete_shift_requirement(session, schedule_id, requirement_id),
        lambda session: _assignment(session, requirement_id, user_id),
    )
    assert first is None and second is LookupError
    with postgres_factory() as session:
        assert (
            _integrity(
                session,
                "INSERT INTO shift_assignments "
                "(shift_requirement_id,user_id,assigned_by) VALUES (:rid,:uid,:uid)",
                {"rid": requirement_id, "uid": user_id},
                "23503",
            )
            is not None
        )
    assert (
        _count(postgres_factory, ShiftAssignment, shift_requirement_id=requirement_id)
        == 0
    )


def test_patch_first_then_assignment_sees_current_requirement(
    postgres_factory, data
) -> None:
    schedule_id, position_id, user_id = data
    requirement_id = _create(postgres_factory, schedule_id, position_id)
    first, second = _ordered(
        postgres_factory,
        lambda session: update_shift_requirement(
            session, schedule_id, requirement_id, changes={"required_workers": 3}
        ),
        lambda session: _assignment(session, requirement_id, user_id),
    )
    assert first[0].required_workers == 3 and second is None
    with postgres_factory() as session:
        assert session.get(ShiftRequirement, requirement_id).required_workers == 3
        assert (
            _count(
                postgres_factory, ShiftAssignment, shift_requirement_id=requirement_id
            )
            == 1
        )
