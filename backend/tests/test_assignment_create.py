"""HTTP and transaction tests for creating one shift assignment."""

import base64
import secrets
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.assignments.models import ShiftAssignment
from src.assignments.repository import ShiftAssignmentRepository
from src.assignments.service import (
    AssignmentAlreadyExists,
    AssignmentOverlap,
    _assignment_db_conflict,
)
from src.auth import session_service
from src.availability.models import AvailabilityEntry, AvailabilitySubmission
from src.core.database import Base, get_session
from src.main import create_app
from src.positions.models import Position
from src.schedules.models import Schedule, ShiftRequirement
from src.schedules.repository import ScheduleRepository
from src.users.models import Role, User

SCHEDULE_ID = 45
OTHER_SCHEDULE_ID = 46
PATH = f"/api/v1/schedules/{SCHEDULE_ID}/shift-assignments"
NOW = datetime(2026, 10, 24, 10, tzinfo=UTC)


@pytest.fixture
def assignment_api(monkeypatch: pytest.MonkeyPatch):
    """Create isolated rows and use real Bearer and role dependencies."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "assignment-create-v1")
    monkeypatch.setenv("JWT_ISSUER", "assignment-create")
    monkeypatch.setenv("JWT_AUDIENCE", "assignment-create-api")
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, record) -> None:
        connection.create_function("btrim", 1, str.strip)
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(
        engine,
        tables=[
            Role.__table__,
            User.__table__,
            Position.__table__,
            Schedule.__table__,
            ShiftRequirement.__table__,
            AvailabilitySubmission.__table__,
            AvailabilityEntry.__table__,
            ShiftAssignment.__table__,
        ],
    )
    with Session(engine) as session:
        session.add_all(
            [
                Role(id=1, code="admin", name="Admin"),
                Role(id=2, code="manager", name="Manager"),
                Role(id=3, code="employee", name="Employee"),
            ]
        )
        session.flush()
        session.add_all(
            [
                User(
                    id=7,
                    role_id=2,
                    email="manager@example.com",
                    full_name="Manager",
                    password_hash="x",
                    is_active=True,
                    activated_at=NOW,
                ),
                User(
                    id=8,
                    role_id=3,
                    email="worker@example.com",
                    full_name="Worker",
                    password_hash="x",
                    is_active=True,
                    activated_at=NOW,
                ),
                User(
                    id=9,
                    role_id=3,
                    email="worker2@example.com",
                    full_name="Worker 2",
                    password_hash="x",
                    is_active=True,
                    activated_at=NOW,
                ),
                User(
                    id=10,
                    role_id=3,
                    email="inactive@example.com",
                    full_name="Inactive",
                    password_hash="x",
                    is_active=False,
                    activated_at=NOW,
                ),
                User(
                    id=11,
                    role_id=3,
                    email="pending@example.com",
                    full_name="Pending",
                    password_hash="x",
                    is_active=False,
                    activated_at=None,
                ),
                User(
                    id=12,
                    role_id=1,
                    email="admin@example.com",
                    full_name="Admin",
                    password_hash="x",
                    is_active=True,
                    activated_at=NOW,
                ),
                User(
                    id=13,
                    role_id=2,
                    email="manager2@example.com",
                    full_name="Manager 2",
                    password_hash="x",
                    is_active=True,
                    activated_at=NOW,
                ),
            ]
        )
        session.add_all(
            [
                Position(id=1, name="Inactive position", is_active=False),
                Position(id=2, name="Active position", is_active=True),
            ]
        )
        session.flush()
        session.add_all(
            [
                Schedule(
                    id=SCHEDULE_ID,
                    year=2026,
                    month=11,
                    status="closed",
                    created_by=7,
                    created_at=NOW,
                ),
                Schedule(
                    id=OTHER_SCHEDULE_ID,
                    year=2026,
                    month=12,
                    status="closed",
                    created_by=7,
                    created_at=NOW,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                ShiftRequirement(
                    id=31,
                    schedule_id=SCHEDULE_ID,
                    position_id=1,
                    work_date=date(2026, 11, 2),
                    start_time=time(8),
                    end_time=time(16),
                    required_workers=3,
                ),
                ShiftRequirement(
                    id=32,
                    schedule_id=SCHEDULE_ID,
                    position_id=2,
                    work_date=date(2026, 11, 2),
                    start_time=time(16),
                    end_time=time(20),
                    required_workers=3,
                ),
                ShiftRequirement(
                    id=33,
                    schedule_id=SCHEDULE_ID,
                    position_id=2,
                    work_date=date(2026, 11, 2),
                    start_time=time(12),
                    end_time=time(18),
                    required_workers=3,
                ),
                ShiftRequirement(
                    id=34,
                    schedule_id=SCHEDULE_ID,
                    position_id=2,
                    work_date=date(2026, 11, 3),
                    start_time=time(8),
                    end_time=time(16),
                    required_workers=3,
                ),
                ShiftRequirement(
                    id=35,
                    schedule_id=OTHER_SCHEDULE_ID,
                    position_id=2,
                    work_date=date(2026, 12, 2),
                    start_time=time(8),
                    end_time=time(16),
                    required_workers=3,
                ),
            ]
        )
        session.commit()

    app = create_app()
    actor = SimpleNamespace(
        id=7, is_active=True, activated_at=NOW, role=SimpleNamespace(code="manager")
    )
    session_id = uuid4()
    auth_session = SimpleNamespace(
        user_id=7,
        revoked_at=None,
        idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
        absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
    )

    class AuthRepository:
        def get_session(self, requested_id):
            return auth_session if requested_id == session_id else None

        def get_user_fresh(self, requested_id):
            return actor if requested_id == 7 else None

    monkeypatch.setattr(
        session_service, "AuthSessionRepository", lambda session: AuthRepository()
    )

    def test_session():
        with Session(engine) as session:
            yield session

    @event.listens_for(Session, "before_flush")
    def sqlite_identity(session, flush_context, instances) -> None:
        if session.bind is not engine:
            return
        for model, initial in (
            (ShiftAssignment, 100),
            (AvailabilitySubmission, 200),
            (AvailabilityEntry, 300),
        ):
            next_id = session.scalar(select(func.max(model.id))) or initial
            for pending in session.new:
                if isinstance(pending, model) and pending.id is None:
                    next_id += 1
                    pending.id = next_id

    app.dependency_overrides[get_session] = test_session
    token = app.state.jwt_codec.issue(7, session_id)
    try:
        yield app, token, actor, engine
    finally:
        app.dependency_overrides.clear()
        event.remove(Session, "before_flush", sqlite_identity)
        engine.dispose()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _body(
    requirement_id: str = "31", user_id: str = "8", acknowledged: bool = False
) -> dict:
    return {
        "shift_requirement_id": requirement_id,
        "user_id": user_id,
        "warning_acknowledged": acknowledged,
    }


def _entries(
    engine,
    *,
    user_id: int = 8,
    submitted: bool = True,
    intervals: list[tuple[date, time, time, str]] | None = None,
) -> None:
    """Seed one monthly submission with requested intervals."""
    with Session(engine) as session:
        submission = AvailabilitySubmission(
            schedule_id=SCHEDULE_ID,
            user_id=user_id,
            submitted_at=NOW if submitted else None,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(submission)
        session.flush()
        for day, start, end, status in intervals or []:
            session.add(
                AvailabilityEntry(
                    submission_id=submission.id,
                    work_date=day,
                    start_time=start,
                    end_time=end,
                    status=status,
                )
            )
        session.commit()


def _count(engine) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(ShiftAssignment))


def _post(app, token, body=None, *, path=PATH):
    with TestClient(app) as client:
        return client.post(
            path,
            json=_body(acknowledged=True) if body is None else body,
            headers=_headers(token),
        )


@pytest.mark.parametrize("acknowledged", [False, True])
def test_available_success_audit_location_and_get_detail(
    assignment_api, acknowledged
) -> None:
    app, token, _, engine = assignment_api
    _entries(engine, intervals=[(date(2026, 11, 2), time(8), time(16), "available")])
    response = _post(app, token, _body(acknowledged=acknowledged))
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["location"].startswith(PATH + "/")
    body = response.json()
    assert body["assigned_by"] == "7"
    assert body["user_id"] == "8"
    assert body["shift_requirement_id"] == "31"
    assert body["warning_type"] is body["warning_acknowledged_at"] is None
    assert body["work_date"] == "2026-11-02"
    assert body["start_time"] == "08:00:00" and body["end_time"] == "16:00:00"
    assert (
        datetime.fromisoformat(body["assigned_at"].replace("Z", "+00:00")).tzinfo
        is not None
    )
    with TestClient(app) as client:
        detail = client.get(response.headers["location"], headers=_headers(token))
    assert detail.status_code == 200 and detail.json() == body
    assert _count(engine) == 1


@pytest.mark.parametrize(
    "intervals, expected",
    [
        (
            [(time(8), time(12), "available"), (time(12), time(16), "available")],
            "available",
        ),
        ([(time(8), time(12), "available")], "unknown"),
        (
            [(time(8), time(11), "available"), (time(12), time(16), "available")],
            "unknown",
        ),
        (
            [(time(7), time(8), "unavailable"), (time(8), time(16), "available")],
            "available",
        ),
        ([(time(16), time(18), "unavailable")], "unknown"),
        ([(time(15), time(18), "unavailable")], "unavailable"),
        (
            [(time(8), time(16), "available"), (time(12), time(14), "unavailable")],
            "unavailable",
        ),
    ],
)
def test_submitted_availability_half_open_coverage(
    assignment_api, intervals, expected
) -> None:
    app, token, _, engine = assignment_api
    _entries(
        engine,
        intervals=[
            (date(2026, 11, 2), start, end, status) for start, end, status in intervals
        ],
    )
    response = _post(app, token)
    assert response.status_code == 201
    assert response.json()["warning_type"] == (
        None if expected == "available" else expected
    )
    if expected != "available":
        assert response.json()["warning_acknowledged_at"] is not None


@pytest.mark.parametrize("submission", ["missing", "empty", "draft", "other_day"])
def test_unknown_without_submitted_matching_intervals(
    assignment_api, submission
) -> None:
    app, token, _, engine = assignment_api
    if submission == "empty":
        _entries(engine)
    elif submission == "draft":
        _entries(
            engine,
            submitted=False,
            intervals=[(date(2026, 11, 2), time(8), time(16), "available")],
        )
    elif submission == "other_day":
        _entries(
            engine, intervals=[(date(2026, 11, 3), time(8), time(16), "available")]
        )
    response = _post(app, token)
    assert response.status_code == 201
    assert response.json()["warning_type"] == "unknown"


@pytest.mark.parametrize(
    "intervals",
    [
        [],
        [(date(2026, 11, 2), time(8), time(10), "unavailable")],
    ],
)
def test_warning_requires_explicit_acknowledgement(assignment_api, intervals) -> None:
    app, token, _, engine = assignment_api
    if intervals:
        _entries(engine, intervals=intervals)
    response = _post(app, token, {"shift_requirement_id": "31", "user_id": "8"})
    assert response.status_code == 409
    assert (
        response.json()["type"]
        == "urn:dreamplanner:problem:assignment-warning-required"
    )
    assert _count(engine) == 0


@pytest.mark.parametrize(
    "target, problem",
    [
        ("10", "assignment-user-ineligible"),
        ("11", "assignment-user-ineligible"),
        ("12", "assignment-user-ineligible"),
        ("13", "assignment-user-ineligible"),
        ("999", None),
    ],
)
def test_target_user_eligibility(assignment_api, target, problem) -> None:
    app, token, _, engine = assignment_api
    response = _post(app, token, _body(user_id=target, acknowledged=True))
    assert response.status_code == (409 if problem else 404)
    if problem:
        assert response.json()["type"] == f"urn:dreamplanner:problem:{problem}"
    assert _count(engine) == 0


@pytest.mark.parametrize(
    "path, body, expected",
    [
        ("/api/v1/schedules/999/shift-assignments", _body(), 404),
        (PATH, _body(requirement_id="999"), 404),
        (PATH, _body(requirement_id="35"), 404),
    ],
)
def test_schedule_and_requirement_scope(assignment_api, path, body, expected) -> None:
    app, token, _, engine = assignment_api
    response = _post(app, token, body, path=path)
    assert response.status_code == expected
    assert _count(engine) == 0


@pytest.mark.parametrize(
    "status", ["draft", "collecting", "collection_closed", "published"]
)
def test_only_closed_schedule_accepts_post(assignment_api, status) -> None:
    app, token, _, engine = assignment_api
    with Session(engine) as session:
        session.get(Schedule, SCHEDULE_ID).status = status
        session.commit()
    response = _post(app, token)
    assert response.status_code == 409
    assert response.json()["type"] == "urn:dreamplanner:problem:schedule-state-conflict"
    assert _count(engine) == 0


def test_capacity_duplicate_and_overlap(assignment_api) -> None:
    app, token, _, engine = assignment_api
    first = _post(app, token)
    duplicate = _post(app, token)
    overlap = _post(app, token, _body(requirement_id="33", acknowledged=True))
    touching = _post(app, token, _body(requirement_id="32", acknowledged=True))
    assert first.status_code == touching.status_code == 201
    assert duplicate.status_code == 409
    assert (
        duplicate.json()["type"] == "urn:dreamplanner:problem:assignment-already-exists"
    )
    assert overlap.status_code == 409
    assert overlap.json()["type"] == "urn:dreamplanner:problem:assignment-overlap"
    with Session(engine) as session:
        session.get(ShiftRequirement, 31).required_workers = 1
        session.commit()
    full = _post(app, token, _body(user_id="9", acknowledged=True))
    assert full.status_code == 409
    assert (
        full.json()["type"] == "urn:dreamplanner:problem:assignment-capacity-exceeded"
    )
    assert _count(engine) == 2


def test_overlap_query_includes_other_schedules_on_same_date(assignment_api) -> None:
    """Keep the read global even if a direct writer violates the monthly model."""
    app, token, _, engine = assignment_api
    with Session(engine) as session:
        session.get(ShiftRequirement, 35).work_date = date(2026, 11, 2)
        session.add(
            ShiftAssignment(
                shift_requirement_id=35,
                user_id=8,
                assigned_by=7,
                assigned_at=NOW,
            )
        )
        session.commit()
    response = _post(app, token)
    assert response.status_code == 409
    assert response.json()["type"] == "urn:dreamplanner:problem:assignment-overlap"
    assert _count(engine) == 1


def test_lock_modes_compile_for_postgresql() -> None:
    """Check the requested row lock strengths without a PostgreSQL server."""
    statements = []

    class RecordingSession:
        def scalar(self, statement):
            statements.append(statement)
            return None

        def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(one_or_none=lambda: None)

    repository = ShiftAssignmentRepository(RecordingSession())
    repository.get_requirement_for_key_share(45, 31)
    repository.get_user_for_share(8)
    sql = [
        str(statement.compile(dialect=postgresql.dialect())) for statement in statements
    ]
    assert "FOR KEY SHARE OF shift_requirements" in sql[0]
    assert "FOR SHARE OF users" in sql[1]


@pytest.mark.parametrize("role", ["admin", "employee"])
def test_actor_role_forbidden_before_resource_lookup(assignment_api, role) -> None:
    app, token, actor, engine = assignment_api
    actor.role.code = role
    response = _post(app, token, path="/api/v1/schedules/999/shift-assignments")
    assert response.status_code == 403
    assert _count(engine) == 0


def test_missing_and_inactive_actor_are_401(assignment_api) -> None:
    app, token, actor, engine = assignment_api
    with TestClient(app) as client:
        missing = client.post(PATH, json=_body())
        actor.is_active = False
        inactive = client.post(PATH, json=_body(), headers=_headers(token))
    for response in (missing, inactive):
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.headers["cache-control"] == "no-store"
    assert _count(engine) == 0


@pytest.mark.parametrize(
    "body",
    [
        {"shift_requirement_id": 31, "user_id": "8"},
        {"shift_requirement_id": 31.0, "user_id": "8"},
        {"shift_requirement_id": True, "user_id": "8"},
        {"shift_requirement_id": "0", "user_id": "8"},
        {"shift_requirement_id": "-1", "user_id": "8"},
        {"shift_requirement_id": None, "user_id": "8"},
        {"shift_requirement_id": "31", "user_id": 8},
        {"shift_requirement_id": "31", "user_id": 8.0},
        {"shift_requirement_id": "31", "user_id": "0"},
        {"shift_requirement_id": "31", "user_id": None},
        {"shift_requirement_id": "31", "user_id": "8", "warning_acknowledged": None},
        {"shift_requirement_id": "31", "user_id": "8", "warning_acknowledged": 1},
        {"shift_requirement_id": "31", "user_id": "8", "assigned_by": "7"},
        {"shift_requirement_id": "31", "user_id": "8", "warning_type": "unknown"},
        {"shift_requirement_id": "31", "user_id": "8", "id": "1"},
        {"shift_requirement_id": "9223372036854775808", "user_id": "8"},
    ],
)
def test_strict_request_validation(assignment_api, body) -> None:
    app, token, _, engine = assignment_api
    response = _post(app, token, body)
    assert response.status_code == 422
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert _count(engine) == 0


def test_malformed_json_is_400(assignment_api) -> None:
    app, token, _, engine = assignment_api
    with TestClient(app) as client:
        response = client.post(
            PATH,
            content=b'{"user_id":',
            headers={**_headers(token), "Content-Type": "application/json"},
        )
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    assert _count(engine) == 0


def test_lock_and_decision_order_one_commit(assignment_api, monkeypatch) -> None:
    app, token, _, engine = assignment_api
    calls = []
    for owner, name in [
        (ScheduleRepository, "get_by_id_for_update"),
        (ShiftAssignmentRepository, "get_requirement_for_key_share"),
        (ShiftAssignmentRepository, "get_user_for_share"),
        (ShiftAssignmentRepository, "count_for_requirement"),
        (ShiftAssignmentRepository, "has_duplicate"),
        (ShiftAssignmentRepository, "has_overlap"),
        (ShiftAssignmentRepository, "submitted_entries"),
        (ShiftAssignmentRepository, "add"),
    ]:
        original = getattr(owner, name)

        def wrapped(self, *args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(owner, name, wrapped)

    def committed(session):
        if session.bind is engine:
            calls.append("commit")

    event.listen(Session, "after_commit", committed)
    try:
        response = _post(app, token)
    finally:
        event.remove(Session, "after_commit", committed)
    assert response.status_code == 201
    assert calls == [
        "get_by_id_for_update",
        "get_requirement_for_key_share",
        "get_user_for_share",
        "count_for_requirement",
        "has_duplicate",
        "has_overlap",
        "submitted_entries",
        "add",
        "commit",
    ]


def test_conflict_rolls_back_without_partial_write(assignment_api) -> None:
    app, token, _, engine = assignment_api
    rollbacks = []

    def rolled_back(session):
        if session.bind is engine:
            rollbacks.append(1)

    event.listen(Session, "after_rollback", rolled_back)
    try:
        response = _post(app, token, {"shift_requirement_id": "31", "user_id": "8"})
    finally:
        event.remove(Session, "after_rollback", rolled_back)
    assert response.status_code == 409
    assert rollbacks == [1]
    assert _count(engine) == 0


def test_only_recognized_database_conflicts_are_mapped() -> None:
    class Diagnostic:
        constraint_name = "uq_shift_assignment"
        message_primary = "Pracownik ma kolidujacy przydzial"

    class Original(Exception):
        sqlstate = "23505"
        diag = Diagnostic()

    from sqlalchemy.exc import DBAPIError

    exc = DBAPIError("insert", {}, Original())
    assert _assignment_db_conflict(exc) is AssignmentAlreadyExists
    Original.sqlstate = "P0001"
    assert _assignment_db_conflict(exc) is AssignmentOverlap
    Original.sqlstate = "23503"
    assert _assignment_db_conflict(exc) is None


def test_openapi_documents_post_without_patch(assignment_api) -> None:
    app, _, _, _ = assignment_api
    paths = app.openapi()["paths"]
    list_path = "/api/v1/schedules/{schedule_id}/shift-assignments"
    detail_path = f"{list_path}/{{assignment_id}}"
    assert set(paths[list_path]) == {"get", "post"}
    assert set(paths[detail_path]) == {"get", "delete"}
    operation = paths[list_path]["post"]
    assert operation["security"] == [{"BearerAuth": []}]
    assert {"201", "400", "401", "403", "404", "409", "422"} <= set(
        operation["responses"]
    )
    assert "requestBody" in operation
    schema = app.openapi()["components"]["schemas"]["CreateShiftAssignmentInput"]
    assert set(schema["properties"]) == {
        "shift_requirement_id",
        "user_id",
        "warning_acknowledged",
    }


@pytest.mark.parametrize(
    "query", ["unexpected=1", "limit=1", "unexpected=1&unexpected=2"]
)
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_non_list_assignment_routes_reject_query(assignment_api, method, query) -> None:
    app, token, _, engine = assignment_api
    path = PATH if method == "POST" else f"{PATH}/999"
    with TestClient(app) as client:
        response = client.request(
            method,
            f"{path}?{query}",
            headers=_headers(token),
            json=_body(acknowledged=True) if method == "POST" else None,
        )
    assert response.status_code == 422
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert _count(engine) == 0


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_non_list_query_does_not_precede_role_check(assignment_api, method) -> None:
    app, token, actor, engine = assignment_api
    actor.role.code = "employee"
    path = PATH if method == "POST" else f"{PATH}/999"
    with TestClient(app) as client:
        response = client.request(
            method,
            f"{path}?unexpected=1",
            headers=_headers(token),
            json=_body(acknowledged=True) if method == "POST" else None,
        )
    assert response.status_code == 403
    assert _count(engine) == 0
