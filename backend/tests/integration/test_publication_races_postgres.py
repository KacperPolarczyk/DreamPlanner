"""Publication races against real PostgreSQL transactions and production services."""

import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from src.assignments.models import ShiftAssignment
from src.assignments.service import (
    AssignmentScheduleStateConflict,
    create_shift_assignment,
    delete_shift_assignment,
)
from src.positions.models import Position
from src.schedules.models import Schedule, ShiftRequirement
from src.schedules.service import (
    SchedulePublicationBlocked,
    SchedulePublicationForbidden,
    ScheduleStateConflict,
    close_collection,
    close_schedule,
    open_collection,
    publish_schedule,
)
from src.users.admin_service import change_role, deactivate_account
from src.users.models import Role, User

DB_NAME = "dreamplanner_test_publication_races_134"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("TEST_DATABASE_URL")
        or not os.getenv("TEST_MIGRATION_DATABASE_URL"),
        reason="Set both TEST_*_DATABASE_URL values to the dedicated database",
    ),
]


@dataclass
class Outcome:
    """Result of one independent PostgreSQL transaction."""

    value: object | None
    error: Exception | None
    backend_pid: int


@pytest.fixture(scope="module")
def database() -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    """Use only the explicitly named, already migrated test database."""
    app_url = make_url(os.environ["TEST_DATABASE_URL"])
    migration_url = make_url(os.environ["TEST_MIGRATION_DATABASE_URL"])
    assert app_url.drivername == migration_url.drivername == "postgresql+psycopg"
    assert app_url.host == migration_url.host == "127.0.0.1"
    assert (app_url.port, app_url.database) == (
        migration_url.port,
        migration_url.database,
    )
    assert app_url.database == DB_NAME
    assert app_url.username == "dreamplanner_app"
    assert migration_url.username == "dreamplanner_migration"
    engine = create_engine(
        app_url,
        hide_parameters=True,
        connect_args={"options": "-c lock_timeout=5000 -c statement_timeout=15000"},
        pool_size=5,
        max_overflow=0,
    )
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_database()")) == DB_NAME
            assert connection.scalar(text("SELECT count(*) FROM alembic_version")) == 1
        yield engine, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture
def case(database: tuple[Engine, sessionmaker[Session]]):
    """Create a separate schedule and actors for each race."""
    _, factory = database

    def create(*, status: str = "closed") -> dict[str, int]:
        with factory() as session:
            role_ids = dict(session.execute(select(Role.code, Role.id)).all())
            last_period = session.execute(
                select(Schedule.year, Schedule.month)
                .order_by(Schedule.year.desc(), Schedule.month.desc())
                .limit(1)
            ).one_or_none()
            last_year, last_month = last_period or (2026, 12)
            year, month = (
                (last_year + 1, 1) if last_month == 12 else (last_year, last_month + 1)
            )
            assert year <= 2100
            manager = User(
                role_id=role_ids["manager"],
                email=f"race-manager-{uuid4()}@example.test",
                full_name="Race Manager",
                password_hash="unused",
                is_active=True,
                activated_at=datetime.now(UTC),
            )
            admin = User(
                role_id=role_ids["admin"],
                email=f"race-admin-{uuid4()}@example.test",
                full_name="Race Admin",
                password_hash="unused",
                is_active=True,
                activated_at=datetime.now(UTC),
            )
            workers = [
                User(
                    role_id=role_ids["employee"],
                    email=f"race-worker-{uuid4()}@example.test",
                    full_name="Race Worker",
                    password_hash="unused",
                    is_active=True,
                    activated_at=datetime.now(UTC),
                )
                for _ in range(2)
            ]
            position = Position(name=f"Race position {uuid4()}", is_active=True)
            schedule = Schedule(year=year, month=month, status=status, creator=manager)
            session.add_all([manager, admin, *workers, position, schedule])
            session.flush()
            requirement = ShiftRequirement(
                schedule_id=schedule.id,
                position_id=position.id,
                work_date=date(year, month, 10),
                start_time=time(9),
                end_time=time(17),
                required_workers=2,
            )
            session.add(requirement)
            session.flush()
            if status == "closed":
                session.add(
                    ShiftAssignment(
                        shift_requirement_id=requirement.id,
                        user_id=workers[0].id,
                        assigned_by=manager.id,
                        assigned_at=datetime.now(UTC),
                    )
                )
            result = {
                "schedule": schedule.id,
                "requirement": requirement.id,
                "manager": manager.id,
                "admin": admin.id,
                "worker": workers[0].id,
                "other_worker": workers[1].id,
            }
            session.commit()
            return result

    return create


def _matches(statement: str, kind: str) -> bool:
    normalized = " ".join(statement.upper().split())
    if kind == "schedule":
        return "FROM SCHEDULES" in normalized and "FOR UPDATE" in normalized
    if kind == "user_update":
        return "FROM USERS" in normalized and "FOR UPDATE" in normalized
    if kind == "user_share":
        return "FROM USERS" in normalized and "FOR SHARE" in normalized
    raise AssertionError(f"Unknown lock kind: {kind}")


def _race(
    database: tuple[Engine, sessionmaker[Session]],
    first: Callable[[Session], object],
    second: Callable[[Session], object],
    *,
    first_lock: str = "schedule",
    second_wait: str = "schedule",
) -> tuple[Outcome, Outcome]:
    """Hold the first service's acquired lock until the second reaches SQL."""
    engine, factory = database
    first_locked = Event()
    second_started = Event()
    release_first = Event()
    backend_pids: dict[str, int] = {}

    def before_execute(conn, cursor, statement, parameters, context, executemany):
        if conn.info.get("race_side") == "second" and _matches(statement, second_wait):
            second_started.set()

    def after_execute(conn, cursor, statement, parameters, context, executemany):
        if conn.info.get("race_side") == "first" and _matches(statement, first_lock):
            first_locked.set()
            if not release_first.wait(8):
                raise TimeoutError("First transaction was not released")

    def invoke(side: str, operation: Callable[[Session], object]) -> Outcome:
        with factory() as session:
            connection = session.connection()
            pid = connection.scalar(text("SELECT pg_backend_pid()"))
            backend_pids[side] = pid
            connection_info = connection.info
            connection_info["race_side"] = side
            try:
                return Outcome(operation(session), None, pid)
            except Exception as exc:
                session.rollback()
                return Outcome(None, exc, pid)
            finally:
                connection_info.pop("race_side", None)

    event.listen(engine, "before_cursor_execute", before_execute)
    event.listen(engine, "after_cursor_execute", after_execute)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(invoke, "first", first)
            try:
                assert first_locked.wait(8), "First operation did not acquire its lock"
                second_future = pool.submit(invoke, "second", second)
                assert second_started.wait(8), "Second operation did not reach its lock"
                with engine.connect() as observer:
                    deadline = monotonic() + 4
                    while monotonic() < deadline:
                        blockers = observer.scalar(
                            text("SELECT pg_blocking_pids(:pid)"),
                            {"pid": backend_pids["second"]},
                        )
                        if backend_pids["first"] in blockers:
                            break
                        Event().wait(0.01)
                    else:
                        raise AssertionError(
                            "Second PostgreSQL backend was not blocked"
                        )
            finally:
                release_first.set()
            outcomes = first_future.result(timeout=20), second_future.result(timeout=20)
        assert outcomes[0].backend_pid != outcomes[1].backend_pid
        return outcomes
    finally:
        event.remove(engine, "before_cursor_execute", before_execute)
        event.remove(engine, "after_cursor_execute", after_execute)


def _state(database, data) -> tuple[str, datetime | None, list[int]]:
    _, factory = database
    with factory() as session:
        schedule = session.get(Schedule, data["schedule"])
        assignments = session.scalars(
            select(ShiftAssignment.user_id)
            .where(ShiftAssignment.shift_requirement_id == data["requirement"])
            .order_by(ShiftAssignment.user_id)
        ).all()
        return schedule.status, schedule.published_at, list(assignments)


def _publish(data):
    return lambda session: publish_schedule(
        session, data["schedule"], actor_id=data["manager"]
    )


@pytest.mark.parametrize("winner", ["post", "publication"])
def test_publication_vs_assignment_post(database, case, winner: str) -> None:
    data = case()

    def create(session: Session) -> object:
        return create_shift_assignment(
            session,
            data["schedule"],
            actor_id=data["manager"],
            actor_role="manager",
            requirement_id=data["requirement"],
            user_id=data["other_worker"],
            warning_acknowledged=True,
        )

    first, second = (
        (create, _publish(data)) if winner == "post" else (_publish(data), create)
    )
    a, b = _race(database, first, second)
    status, published_at, users = _state(database, data)
    assert status == "published" and published_at is not None
    if winner == "post":
        assert a.error is None and b.error is None
        assert users == sorted([data["worker"], data["other_worker"]])
    else:
        assert a.error is None and isinstance(b.error, AssignmentScheduleStateConflict)
        assert users == [data["worker"]]
    assert len(users) <= 2


@pytest.mark.parametrize("winner", ["delete", "publication"])
def test_publication_vs_assignment_delete(database, case, winner: str) -> None:
    data = case()
    _, factory = database
    with factory() as session:
        assignment_id = session.scalar(
            select(ShiftAssignment.id).where(
                ShiftAssignment.shift_requirement_id == data["requirement"]
            )
        )

    def delete(session: Session) -> None:
        delete_shift_assignment(
            session, data["schedule"], assignment_id, actor_role="manager"
        )

    first, second = (
        (delete, _publish(data)) if winner == "delete" else (_publish(data), delete)
    )
    a, b = _race(database, first, second)
    status, published_at, users = _state(database, data)
    assert status == "published" and published_at is not None
    if winner == "delete":
        assert a.error is None and b.error is None and users == []
    else:
        assert a.error is None and isinstance(b.error, AssignmentScheduleStateConflict)
        assert users == [data["worker"]]


def test_two_publications_set_timestamp_once(database, case) -> None:
    data = case()
    first, second = _race(database, _publish(data), _publish(data))
    assert first.error is None
    assert isinstance(second.error, ScheduleStateConflict)
    status, published_at, users = _state(database, data)
    assert status == "published"
    assert published_at == first.value.published_at
    assert users == [data["worker"]]


@pytest.mark.parametrize("winner", ["transition", "publication"])
@pytest.mark.parametrize(
    ("initial_status", "target_status", "transition_service"),
    [
        ("draft", "collecting", open_collection),
        ("collecting", "collection_closed", close_collection),
        ("collection_closed", "closed", close_schedule),
    ],
)
def test_publication_vs_status_transition(
    database,
    case,
    winner: str,
    initial_status: str,
    target_status: str,
    transition_service,
) -> None:
    data = case(status=initial_status)

    def transition(session: Session) -> Schedule:
        return transition_service(session, data["schedule"])

    first, second = (
        (transition, _publish(data))
        if winner == "transition"
        else (_publish(data), transition)
    )
    a, b = _race(database, first, second)
    status, published_at, users = _state(database, data)
    assert users == []
    if initial_status == "collection_closed" and winner == "transition":
        assert a.error is None and b.error is None
        assert status == "published" and published_at is not None
    else:
        if winner == "transition":
            assert a.error is None and isinstance(b.error, ScheduleStateConflict)
        else:
            assert isinstance(a.error, ScheduleStateConflict) and b.error is None
        assert status == target_status and published_at is None


@pytest.mark.parametrize("winner", ["admin", "publication"])
@pytest.mark.parametrize("action", ["deactivate", "change_role"])
def test_publication_vs_employee_admin_change(
    database, case, winner: str, action: str
) -> None:
    data = case()
    admin = (
        (lambda session: deactivate_account(session, data["admin"], data["worker"]))
        if action == "deactivate"
        else (
            lambda session: change_role(
                session, data["admin"], data["worker"], "manager"
            )
        )
    )
    if winner == "admin":
        a, b = _race(
            database,
            admin,
            _publish(data),
            first_lock="user_update",
            second_wait="user_share",
        )
        assert a.error is None and isinstance(b.error, SchedulePublicationBlocked)
        assert b.error.reason == "ineligible_user"
        expected_status = "closed"
    else:
        a, b = _race(
            database,
            _publish(data),
            admin,
            first_lock="user_share",
            second_wait="user_update",
        )
        assert a.error is None and b.error is None
        expected_status = "published"
    status, published_at, users = _state(database, data)
    assert status == expected_status
    assert (published_at is not None) == (winner == "publication")
    assert users == [data["worker"]]
    _, factory = database
    with factory() as session:
        worker = session.get(User, data["worker"])
        if action == "deactivate":
            assert worker.is_active is False
        else:
            assert worker.role.code == "manager"


@pytest.mark.parametrize("winner", ["admin", "publication"])
@pytest.mark.parametrize("action", ["deactivate", "change_role"])
def test_publication_vs_manager_admin_change(
    database, case, winner: str, action: str
) -> None:
    data = case()
    admin = (
        (lambda session: deactivate_account(session, data["admin"], data["manager"]))
        if action == "deactivate"
        else (
            lambda session: change_role(
                session, data["admin"], data["manager"], "employee"
            )
        )
    )
    if winner == "admin":
        a, b = _race(
            database,
            admin,
            _publish(data),
            first_lock="user_update",
            second_wait="user_share",
        )
        assert a.error is None and isinstance(b.error, SchedulePublicationForbidden)
        expected_status = "closed"
    else:
        a, b = _race(
            database,
            _publish(data),
            admin,
            first_lock="user_share",
            second_wait="user_update",
        )
        assert a.error is None and b.error is None
        expected_status = "published"
    status, published_at, users = _state(database, data)
    assert status == expected_status
    assert (published_at is not None) == (winner == "publication")
    assert users == [data["worker"]]
    _, factory = database
    with factory() as session:
        manager = session.get(User, data["manager"])
        if action == "deactivate":
            assert manager.is_active is False
        else:
            assert manager.role.code == "employee"


def test_publication_rollback_after_locked_update(database, case) -> None:
    data = case()
    engine, factory = database
    injected = Event()

    def fail_after_update(conn, cursor, statement, parameters, context, executemany):
        if statement.upper().lstrip().startswith("UPDATE SCHEDULES SET"):
            injected.set()
            raise RuntimeError("injected failure after publication update")

    event.listen(engine, "after_cursor_execute", fail_after_update)
    try:
        with (
            factory() as session,
            pytest.raises(RuntimeError, match="injected failure"),
        ):
            publish_schedule(session, data["schedule"], actor_id=data["manager"])
    finally:
        event.remove(engine, "after_cursor_execute", fail_after_update)
    assert injected.is_set()
    assert _state(database, data) == ("closed", None, [data["worker"]])
    with factory() as session:
        schedule = session.scalar(
            select(Schedule)
            .where(Schedule.id == data["schedule"])
            .with_for_update(nowait=True)
        )
        assert schedule.status == "closed"
        session.rollback()
    with factory() as session:
        result = publish_schedule(session, data["schedule"], actor_id=data["manager"])
        assert result.status == "published"
    assert _state(database, data)[0] == "published"
