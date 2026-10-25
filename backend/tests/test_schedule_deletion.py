"""SQLite HTTP checks for safe deletion of monthly draft schedules."""

import base64
import secrets
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.assignments.models import ShiftAssignment
from src.auth import session_service
from src.availability.models import AvailabilityEntry, AvailabilitySubmission
from src.core.database import get_session
from src.core.models import Base
from src.main import create_app
from src.positions.models import Position
from src.schedules.models import Schedule, ShiftRequirement
from src.schedules.repository import ScheduleRepository
from src.users.models import Role, User

PATH = "/api/v1/schedules"
SCHEDULE_ID = 10
CREATOR_ID = 42


@pytest.fixture
def deletion_api(monkeypatch: pytest.MonkeyPatch):
    """Use SQLite FK enforcement to detect an unintended cascade delete."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "schedule-deletion-test-v1")
    monkeypatch.setenv("JWT_ISSUER", "dreamplanner-schedule-deletion-test")
    monkeypatch.setenv("JWT_AUDIENCE", "dreamplanner-schedule-deletion-api")
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, record) -> None:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.create_function("btrim", 1, str.strip)

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Role(id=1, code="manager", name="Manager"))
        session.add(
            User(
                id=CREATOR_ID,
                role_id=1,
                email="manager@example.test",
                full_name="Manager",
                password_hash="unused",
                is_active=True,
                activated_at=datetime.now(UTC),
            )
        )
        session.add(Position(id=1, name="Kitchen", is_active=True))
        session.add(
            Schedule(
                id=SCHEDULE_ID,
                year=2027,
                month=1,
                status="draft",
                created_by=CREATOR_ID,
            )
        )
        session.commit()

    next_id = SCHEDULE_ID + 1

    def assign_sqlite_identity(mapper, connection, schedule) -> None:
        """Simulate PostgreSQL BIGINT identity for a later POST."""
        nonlocal next_id
        if schedule.id is None:
            schedule.id = next_id
            next_id += 1

    event.listen(Schedule, "before_insert", assign_sqlite_identity)
    app = create_app()
    session_id = uuid4()
    actor = SimpleNamespace(
        id=CREATOR_ID,
        is_active=True,
        activated_at=datetime.now(UTC),
        role=SimpleNamespace(code="manager"),
    )
    auth_session = SimpleNamespace(
        user_id=CREATOR_ID,
        revoked_at=None,
        idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
        absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
    )

    class AuthRepository:
        """Supply current identity for the real Bearer dependency."""

        def get_session(self, requested_id):
            return auth_session if requested_id == session_id else None

        def get_user_fresh(self, requested_id):
            return actor if requested_id == CREATOR_ID else None

    monkeypatch.setattr(
        session_service, "AuthSessionRepository", lambda session: AuthRepository()
    )

    def test_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    token = app.state.jwt_codec.issue(CREATOR_ID, session_id)
    yield app, token, actor, engine
    app.dependency_overrides.clear()
    event.remove(Schedule, "before_insert", assign_sqlite_identity)
    engine.dispose()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _delete(client: TestClient, token: str, schedule_id: str = "10"):
    return client.delete(f"{PATH}/{schedule_id}", headers=_headers(token))


def _count(engine, model) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def _add_requirement(engine) -> None:
    with Session(engine) as session:
        session.add(
            ShiftRequirement(
                id=20,
                schedule_id=SCHEDULE_ID,
                position_id=1,
                work_date=date(2027, 1, 8),
                start_time=time(9),
                end_time=time(17),
                required_workers=1,
            )
        )
        session.commit()


def _add_submission(engine) -> None:
    with Session(engine) as session:
        session.add(
            AvailabilitySubmission(id=30, schedule_id=SCHEDULE_ID, user_id=CREATOR_ID)
        )
        session.commit()


def test_delete_empty_draft_and_recreate_period(deletion_api) -> None:
    app, token, _, engine = deletion_api
    with TestClient(app) as client:
        deleted = _delete(client, token)
        assert _count(engine, Schedule) == 0
        missing = _delete(client, token)
        recreated = client.post(
            PATH, json={"year": 2027, "month": 1}, headers=_headers(token)
        )
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert deleted.headers["cache-control"] == "no-store"
    assert missing.status_code == 404
    assert missing.json()["type"] == "about:blank"
    assert recreated.status_code == 201
    assert recreated.json()["id"] != str(SCHEDULE_ID)
    assert _count(engine, Schedule) == 1


def test_lock_precedes_both_fresh_child_checks(deletion_api) -> None:
    app, token, _, _ = deletion_api
    statements = []

    def capture_select(state) -> None:
        if state.is_select:
            statements.append(
                str(state.statement.compile(dialect=postgresql.dialect()))
            )

    event.listen(Session, "do_orm_execute", capture_select)
    try:
        with TestClient(app) as client:
            response = _delete(client, token)
    finally:
        event.remove(Session, "do_orm_execute", capture_select)
    assert response.status_code == 204
    assert "FROM users" in statements[0]
    assert "FOR SHARE OF users" in statements[0]
    assert "FROM schedules" in statements[1]
    assert "FOR UPDATE" in statements[1]
    assert "FROM shift_requirements" in statements[2]
    assert "FROM availability_submissions" in statements[3]


@pytest.mark.parametrize(
    "status", ["collecting", "collection_closed", "closed", "published"]
)
def test_non_draft_is_state_conflict(deletion_api, status) -> None:
    app, token, _, engine = deletion_api
    with Session(engine) as session:
        schedule = session.get(Schedule, SCHEDULE_ID)
        schedule.status = status
        session.commit()
    with TestClient(app) as client:
        response = _delete(client, token)
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": "urn:dreamplanner:problem:schedule-state-conflict",
        "title": "Nie można usunąć grafiku",
        "status": 409,
        "detail": "Usunąć można tylko grafik w stanie draft.",
    }
    assert _count(engine, Schedule) == 1


@pytest.mark.parametrize("child", ["requirement", "submission", "both"])
def test_children_block_delete_without_cascade(deletion_api, child) -> None:
    app, token, _, engine = deletion_api
    if child in {"requirement", "both"}:
        _add_requirement(engine)
    if child in {"submission", "both"}:
        _add_submission(engine)
    with TestClient(app) as client:
        response = _delete(client, token)
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": "urn:dreamplanner:problem:schedule-not-empty",
        "title": "Grafik nie jest pusty",
        "status": 409,
        "detail": "Grafik zawiera dane powiązane i nie może zostać usunięty.",
    }
    assert _count(engine, Schedule) == 1
    assert _count(engine, ShiftRequirement) == (child in {"requirement", "both"})
    assert _count(engine, AvailabilitySubmission) == (child in {"submission", "both"})


def test_indirect_children_are_preserved(deletion_api) -> None:
    app, token, _, engine = deletion_api
    _add_submission(engine)
    with Session(engine) as session:
        session.add(
            AvailabilityEntry(
                id=40,
                submission_id=30,
                work_date=date(2027, 1, 8),
                start_time=time(9),
                end_time=time(17),
                status="available",
            )
        )
        session.commit()
    with TestClient(app) as client:
        response = _delete(client, token)
    assert response.status_code == 409
    assert _count(engine, Schedule) == 1
    assert _count(engine, AvailabilitySubmission) == 1
    assert _count(engine, AvailabilityEntry) == 1


def test_shift_assignments_are_preserved(deletion_api) -> None:
    app, token, _, engine = deletion_api
    _add_requirement(engine)
    with Session(engine) as session:
        session.add(
            ShiftAssignment(
                id=50,
                shift_requirement_id=20,
                user_id=CREATOR_ID,
                assigned_by=CREATOR_ID,
            )
        )
        session.commit()
    with TestClient(app) as client:
        response = _delete(client, token)
    assert response.status_code == 409
    assert _count(engine, Schedule) == 1
    assert _count(engine, ShiftRequirement) == 1
    assert _count(engine, ShiftAssignment) == 1


def test_state_conflict_precedes_child_check(deletion_api) -> None:
    app, token, _, engine = deletion_api
    _add_requirement(engine)
    with Session(engine) as session:
        schedule = session.get(Schedule, SCHEDULE_ID)
        schedule.status = "collecting"
        session.commit()
    with TestClient(app) as client:
        response = _delete(client, token)
    assert response.json()["type"] == "urn:dreamplanner:problem:schedule-state-conflict"
    assert _count(engine, ShiftRequirement) == 1


@pytest.mark.parametrize("schedule_id", ["0", "999"])
def test_missing_schedule_is_404(deletion_api, schedule_id) -> None:
    app, token, _, engine = deletion_api
    with TestClient(app) as client:
        response = _delete(client, token, schedule_id)
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["type"] == "about:blank"
    assert _count(engine, Schedule) == 1


@pytest.mark.parametrize(
    "schedule_id", ["invalid", "-1", "１２", "9223372036854775808"]
)
def test_invalid_schedule_id_is_422(deletion_api, schedule_id) -> None:
    app, token, _, engine = deletion_api
    with TestClient(app) as client:
        response = _delete(client, token, schedule_id)
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert response.json()["errors"]
    assert _count(engine, Schedule) == 1


@pytest.mark.parametrize("content", [b"{}", b"null", b" ", b"text"])
def test_nonempty_body_is_422(deletion_api, content) -> None:
    app, token, _, engine = deletion_api
    with TestClient(app) as client:
        response = client.request(
            "DELETE", f"{PATH}/{SCHEDULE_ID}", content=content, headers=_headers(token)
        )
    assert response.status_code == 422
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert _count(engine, Schedule) == 1


@pytest.mark.parametrize("role", ["admin", "employee"])
def test_other_roles_are_forbidden_before_lookup(deletion_api, role) -> None:
    app, token, actor, engine = deletion_api
    actor.role.code = role
    with TestClient(app) as client:
        responses = [
            _delete(client, token, schedule_id) for schedule_id in ("10", "999")
        ]
    assert [r.status_code for r in responses] == [403, 403]
    assert all(
        r.headers["content-type"] == "application/problem+json" for r in responses
    )
    assert all(r.headers["cache-control"] == "no-store" for r in responses)
    assert _count(engine, Schedule) == 1


def test_no_token_is_401(deletion_api) -> None:
    app, _, _, engine = deletion_api
    with TestClient(app) as client:
        response = client.delete(f"{PATH}/{SCHEDULE_ID}")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"
    assert _count(engine, Schedule) == 1


def test_unexpected_integrity_error_is_safe_500_and_rolls_back(
    deletion_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, token, _, engine = deletion_api
    rollbacks = []
    original_rollback = Session.rollback

    def record_rollback(session) -> None:
        rollbacks.append(True)
        original_rollback(session)

    def fail_delete(repository, schedule) -> None:
        raise IntegrityError("DELETE private_sql", {}, Exception("private_db_detail"))

    with monkeypatch.context() as patch:
        patch.setattr(Session, "rollback", record_rollback)
        patch.setattr(ScheduleRepository, "delete", fail_delete)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = _delete(client, token)
    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "about:blank"
    assert "private_" not in response.text
    assert rollbacks
    assert _count(engine, Schedule) == 1
    with TestClient(app) as client:
        retry = _delete(client, token)
    assert retry.status_code == 204


def test_openapi_documents_deletion(deletion_api) -> None:
    app, _, _, _ = deletion_api
    operation = app.openapi()["paths"][f"{PATH}/{{schedule_id}}"]["delete"]
    assert operation["security"] == [{"BearerAuth": []}]
    assert "`manager`" in operation["description"]
    assert {"204", "401", "403", "404", "409", "422"} <= set(operation["responses"])
    assert "content" not in operation["responses"]["204"]
    assert "requestBody" not in operation
    for status in ("401", "403", "404", "409", "422"):
        assert "application/problem+json" in operation["responses"][status]["content"]
