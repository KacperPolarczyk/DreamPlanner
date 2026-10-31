"""SQLite HTTP checks for schedule hard deletion and employee listing."""

import base64
import secrets
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
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
    """Use SQLite FK enforcement to verify cascades and HTTP contracts."""
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


def _set_role(actor, engine, role: str) -> None:
    """Keep the mocked identity and the persisted account role aligned."""
    role_ids = {"manager": 1, "admin": 3, "employee": 2}
    actor.role.code = role
    with Session(engine) as session:
        if role != "manager" and session.get(Role, role_ids[role]) is None:
            session.add(Role(id=role_ids[role], code=role, name=role.title()))
        session.get(User, CREATOR_ID).role_id = role_ids[role]
        session.commit()


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


@pytest.mark.parametrize("role", ["admin", "manager"])
def test_empty_draft_delete(deletion_api, role) -> None:
    app, token, actor, engine = deletion_api
    _set_role(actor, engine, role)
    with TestClient(app) as client:
        response = _delete(client, token)
        missing = _delete(client, token)
    assert response.status_code == 204 and response.content == b""
    assert missing.status_code == 404
    assert _count(engine, Schedule) == 0


@pytest.mark.parametrize(
    "role,status,children",
    [
        ("manager", "draft", "requirement"),
        ("admin", "collecting", "submission"),
        ("manager", "closed", "assignment"),
        ("admin", "published", "assignment"),
        ("manager", "collection_closed", "both"),
    ],
)
def test_hard_delete_all_states_and_children(
    deletion_api, role, status, children
) -> None:
    app, token, actor, engine = deletion_api
    _set_role(actor, engine, role)
    with Session(engine) as session:
        session.get(Schedule, SCHEDULE_ID).status = status
        session.commit()
    if children in {"requirement", "assignment", "both"}:
        _add_requirement(engine)
    if children in {"submission", "both"}:
        _add_submission(engine)
    with Session(engine) as session:
        if children == "assignment":
            session.add(
                ShiftAssignment(
                    id=50,
                    shift_requirement_id=20,
                    user_id=CREATOR_ID,
                    assigned_by=CREATOR_ID,
                )
            )
        if children in {"submission", "both"}:
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
            session.get(AvailabilitySubmission, 30).submitted_at = datetime.now(UTC)
        session.commit()
    with TestClient(app) as client:
        response = _delete(client, token)
    assert response.status_code == 204
    for model in (
        Schedule,
        ShiftRequirement,
        AvailabilitySubmission,
        AvailabilityEntry,
        ShiftAssignment,
    ):
        assert _count(engine, model) == 0
    assert _count(engine, User) == 1
    assert _count(engine, Position) == 1
    assert _count(engine, Role) == (2 if role == "admin" else 1)


def test_employee_forbidden_before_lookup(deletion_api) -> None:
    app, token, actor, engine = deletion_api
    _set_role(actor, engine, "employee")
    with TestClient(app) as client:
        assert _delete(client, token).status_code == 403
        assert _delete(client, token, "999").status_code == 403
    assert _count(engine, Schedule) == 1


@pytest.mark.parametrize("schedule_id", ["0", "999"])
def test_missing_schedule_is_404(deletion_api, schedule_id) -> None:
    app, token, _, engine = deletion_api
    with TestClient(app) as client:
        response = _delete(client, token, schedule_id)
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert _count(engine, Schedule) == 1


@pytest.mark.parametrize(
    "schedule_id", ["invalid", "-1", "１２", "9223372036854775808"]
)
def test_invalid_schedule_id_is_422(deletion_api, schedule_id) -> None:
    app, token, _, engine = deletion_api
    with TestClient(app) as client:
        response = _delete(client, token, schedule_id)
    assert response.status_code == 422
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert _count(engine, Schedule) == 1


@pytest.mark.parametrize("content", [b"{}", b"null", b" ", b"text"])
def test_nonempty_body_is_422(deletion_api, content) -> None:
    app, token, _, engine = deletion_api
    with TestClient(app) as client:
        response = client.request(
            "DELETE", f"{PATH}/{SCHEDULE_ID}", content=content, headers=_headers(token)
        )
    assert response.status_code == 422
    assert _count(engine, Schedule) == 1


def test_no_token_is_401(deletion_api) -> None:
    app, _, _, engine = deletion_api
    with TestClient(app) as client:
        response = client.delete(f"{PATH}/{SCHEDULE_ID}")
    assert response.status_code == 401
    assert _count(engine, Schedule) == 1


def test_failure_after_flush_rolls_back_cascade(deletion_api, monkeypatch) -> None:
    app, token, _, engine = deletion_api
    _add_requirement(engine)
    original_delete = ScheduleRepository.delete

    def fail_after_flush(repository, schedule) -> None:
        original_delete(repository, schedule)
        repository.session.flush()
        raise IntegrityError("DELETE private_sql", {}, Exception("private_db_detail"))

    with monkeypatch.context() as patch:
        patch.setattr(ScheduleRepository, "delete", fail_after_flush)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = _delete(client, token)
    assert response.status_code == 500
    assert "private_" not in response.text
    assert _count(engine, Schedule) == 1
    assert _count(engine, ShiftRequirement) == 1


def test_published_shifts_disappear_after_deletion(deletion_api) -> None:
    app, token, actor, engine = deletion_api
    _add_requirement(engine)
    with Session(engine) as session:
        session.get(Schedule, SCHEDULE_ID).status = "published"
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
        _set_role(actor, engine, "employee")
        before = client.get("/api/v1/me/shifts", headers=_headers(token))
        _set_role(actor, engine, "admin")
        deleted = _delete(client, token)
        _set_role(actor, engine, "employee")
        after = client.get("/api/v1/me/shifts", headers=_headers(token))
    assert before.status_code == 200 and before.json()["total"] == 1
    assert deleted.status_code == 204
    assert after.status_code == 200 and after.json()["items"] == []


def test_openapi_documents_deletion(deletion_api) -> None:
    app, _, _, _ = deletion_api
    operation = app.openapi()["paths"][f"{PATH}/{{schedule_id}}"]["delete"]
    assert operation["security"] == [{"BearerAuth": []}]
    assert "`admin`" in operation["description"]
    assert "`manager`" in operation["description"]
    assert {"204", "401", "403", "404", "422"} <= set(operation["responses"])
    assert "content" not in operation["responses"]["204"]


@pytest.mark.parametrize("role", ["admin", "manager"])
def test_existing_schedule_list_rbac_unchanged(deletion_api, role) -> None:
    app, token, actor, engine = deletion_api
    _set_role(actor, engine, role)
    with TestClient(app) as client:
        assert client.get(PATH, headers=_headers(token)).status_code == 200
    _set_role(actor, engine, "employee")
    with TestClient(app) as client:
        assert client.get(PATH, headers=_headers(token)).status_code == 403


def test_employee_collecting_list_sort_pagination_and_safe_fields(deletion_api) -> None:
    app, token, actor, engine = deletion_api
    with Session(engine) as session:
        session.add_all(
            [
                Schedule(
                    id=11,
                    year=2027,
                    month=2,
                    status="collecting",
                    created_by=CREATOR_ID,
                ),
                Schedule(
                    id=12,
                    year=2027,
                    month=3,
                    status="collecting",
                    created_by=CREATOR_ID,
                ),
                Schedule(
                    id=13, year=2027, month=4, status="closed", created_by=CREATOR_ID
                ),
                Schedule(
                    id=14,
                    year=2026,
                    month=12,
                    status="collecting",
                    created_by=CREATOR_ID,
                ),
            ]
        )
        session.commit()
    _add_submission(engine)
    _set_role(actor, engine, "employee")
    with TestClient(app) as client:
        first = client.get("/api/v1/me/schedules?limit=2", headers=_headers(token))
        second = client.get(
            "/api/v1/me/schedules?limit=2&offset=2", headers=_headers(token)
        )
        empty = client.get("/api/v1/me/schedules?offset=4", headers=_headers(token))
    assert first.status_code == 200
    assert first.json() == {
        "items": [
            {"id": "12", "year": 2027, "month": 3, "status": "collecting"},
            {"id": "11", "year": 2027, "month": 2, "status": "collecting"},
        ],
        "total": 3,
        "limit": 2,
        "offset": 0,
    }
    assert second.json()["items"] == [
        {"id": "14", "year": 2026, "month": 12, "status": "collecting"}
    ]
    assert empty.json()["items"] == []
    assert set(first.json()["items"][0]) == {"id", "year", "month", "status"}


def test_employee_empty_list_and_auth(deletion_api) -> None:
    app, token, actor, engine = deletion_api
    _set_role(actor, engine, "employee")
    with TestClient(app) as client:
        ok = client.get("/api/v1/me/schedules", headers=_headers(token))
        unauthenticated = client.get("/api/v1/me/schedules")
    assert ok.json() == {"items": [], "total": 0, "limit": 25, "offset": 0}
    assert unauthenticated.status_code == 401


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=101",
        "offset=-1",
        "limit=abc",
        "offset=１２",
        "limit=2&limit=3",
        "status=published",
    ],
)
def test_employee_list_invalid_query(deletion_api, query) -> None:
    app, token, actor, engine = deletion_api
    _set_role(actor, engine, "employee")
    with TestClient(app) as client:
        response = client.get(f"/api/v1/me/schedules?{query}", headers=_headers(token))
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"


def test_employee_list_openapi(deletion_api) -> None:
    app, _, _, _ = deletion_api
    operation = app.openapi()["paths"]["/api/v1/me/schedules"]["get"]
    assert operation["security"] == [{"BearerAuth": []}]
    assert "`employee`" in operation["description"]
    assert {"200", "401", "403", "422"} <= set(operation["responses"])


@pytest.mark.parametrize("role", ["admin", "manager"])
def test_employee_schedule_list_rejects_other_roles(deletion_api, role) -> None:
    app, token, actor, engine = deletion_api
    _set_role(actor, engine, role)
    with TestClient(app) as client:
        response = client.get("/api/v1/me/schedules", headers=_headers(token))
    assert response.status_code == 403
    assert response.headers["content-type"] == "application/problem+json"


def test_deletion_preserves_other_schedule_and_its_requirement(deletion_api) -> None:
    app, token, _, engine = deletion_api
    _add_requirement(engine)
    with Session(engine) as session:
        session.add(
            Schedule(id=11, year=2027, month=2, status="draft", created_by=CREATOR_ID)
        )
        session.add(
            ShiftRequirement(
                id=21,
                schedule_id=11,
                position_id=1,
                work_date=date(2027, 2, 8),
                start_time=time(9),
                end_time=time(17),
                required_workers=1,
            )
        )
        session.commit()
    with TestClient(app) as client:
        response = _delete(client, token)
    assert response.status_code == 204
    with Session(engine) as session:
        assert session.get(Schedule, SCHEDULE_ID) is None
        assert session.get(ShiftRequirement, 20) is None
        assert session.get(Schedule, 11) is not None
        assert session.get(ShiftRequirement, 21) is not None
