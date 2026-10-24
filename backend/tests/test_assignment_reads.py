"""HTTP read contract for schedule-scoped shift assignments."""

import base64
import secrets
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.assignments.models import ShiftAssignment
from src.auth import session_service
from src.core.database import Base, get_session
from src.main import create_app
from src.schedules.models import Schedule, ShiftRequirement

SCHEDULE_ID = 45
OTHER_SCHEDULE_ID = 46
LARGE_ID = 9007199254740993
NOW = datetime(2026, 10, 24, 10, tzinfo=UTC)
PATH = f"/api/v1/schedules/{SCHEDULE_ID}/shift-assignments"
FIELDS = {
    "id",
    "schedule_id",
    "shift_requirement_id",
    "user_id",
    "assigned_by",
    "assigned_at",
    "warning_type",
    "warning_acknowledged_at",
    "work_date",
    "start_time",
    "end_time",
}


@pytest.fixture
def assignment_api(monkeypatch: pytest.MonkeyPatch):
    """Create read-only fixture data with real JWT/session role dependencies."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "assignment-reads-v1")
    monkeypatch.setenv("JWT_ISSUER", "assignment-reads")
    monkeypatch.setenv("JWT_AUDIENCE", "assignment-reads-api")
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def disable_fk(connection, record) -> None:
        connection.execute("PRAGMA foreign_keys=OFF")

    Base.metadata.create_all(
        engine,
        tables=[
            Schedule.__table__,
            ShiftRequirement.__table__,
            ShiftAssignment.__table__,
        ],
    )
    with Session(engine) as session:
        session.add_all(
            [
                Schedule(
                    id=SCHEDULE_ID,
                    year=2026,
                    month=11,
                    status="draft",
                    created_by=7,
                    created_at=NOW,
                ),
                Schedule(
                    id=OTHER_SCHEDULE_ID,
                    year=2026,
                    month=12,
                    status="published",
                    created_by=7,
                    created_at=NOW,
                ),
                Schedule(
                    id=47,
                    year=2027,
                    month=1,
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
                    position_id=1,
                    work_date=date(2026, 11, 1),
                    start_time=time(9),
                    end_time=time(12),
                    required_workers=1,
                ),
                ShiftRequirement(
                    id=33,
                    schedule_id=SCHEDULE_ID,
                    position_id=1,
                    work_date=date(2026, 11, 2),
                    start_time=time(8),
                    end_time=time(10),
                    required_workers=1,
                ),
                ShiftRequirement(
                    id=34,
                    schedule_id=OTHER_SCHEDULE_ID,
                    position_id=1,
                    work_date=date(2026, 12, 1),
                    start_time=time(8),
                    end_time=time(16),
                    required_workers=1,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                ShiftAssignment(
                    id=101,
                    shift_requirement_id=31,
                    user_id=LARGE_ID,
                    assigned_by=7,
                    assigned_at=NOW,
                    warning_type="unknown",
                    warning_acknowledged_at=NOW,
                ),
                ShiftAssignment(
                    id=102,
                    shift_requirement_id=31,
                    user_id=8,
                    assigned_by=7,
                    assigned_at=NOW,
                    warning_type="unavailable",
                    warning_acknowledged_at=NOW,
                ),
                ShiftAssignment(
                    id=LARGE_ID,
                    shift_requirement_id=32,
                    user_id=9,
                    assigned_by=7,
                    assigned_at=NOW,
                ),
                ShiftAssignment(
                    id=103,
                    shift_requirement_id=33,
                    user_id=10,
                    assigned_by=7,
                    assigned_at=NOW,
                ),
                ShiftAssignment(
                    id=104,
                    shift_requirement_id=34,
                    user_id=11,
                    assigned_by=7,
                    assigned_at=NOW,
                ),
            ]
        )
        session.commit()

    app = create_app()
    session_id = uuid4()
    actor = SimpleNamespace(
        id=42, is_active=True, activated_at=NOW, role=SimpleNamespace(code="manager")
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
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    token = app.state.jwt_codec.issue(42, session_id)
    try:
        yield app, token, actor, engine
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_list_and_detail_rbac_response(assignment_api, role) -> None:
    app, token, actor, _ = assignment_api
    actor.role.code = role
    with TestClient(app) as client:
        listing = client.get(PATH, headers=_headers(token))
        detail = client.get(f"{PATH}/101", headers=_headers(token))
    assert listing.status_code == detail.status_code == 200
    assert (
        listing.headers["cache-control"]
        == detail.headers["cache-control"]
        == "no-store"
    )
    assert listing.json()["total"] == 4
    assert [item["id"] for item in listing.json()["items"]] == [
        str(LARGE_ID),
        "101",
        "102",
        "103",
    ]
    assert detail.json() == {
        "id": "101",
        "schedule_id": "45",
        "shift_requirement_id": "31",
        "user_id": str(LARGE_ID),
        "assigned_by": "7",
        "assigned_at": "2026-10-24T10:00:00Z",
        "warning_type": "unknown",
        "warning_acknowledged_at": "2026-10-24T10:00:00Z",
        "work_date": "2026-11-02",
        "start_time": "08:00:00",
        "end_time": "16:00:00",
    }
    assert all(set(item) == FIELDS for item in listing.json()["items"])
    assert [item["warning_type"] for item in listing.json()["items"]] == [
        None,
        "unknown",
        "unavailable",
        None,
    ]
    assert listing.json()["items"][0]["warning_acknowledged_at"] is None


def test_list_filters_pagination_and_scope(assignment_api) -> None:
    app, token, _, _ = assignment_api
    with TestClient(app) as client:
        page = client.get(
            PATH, params={"limit": "1", "offset": "2"}, headers=_headers(token)
        )
        max_limit = client.get(PATH, params={"limit": "100"}, headers=_headers(token))
        end = client.get(PATH, params={"offset": "99"}, headers=_headers(token))
        by_date = client.get(
            PATH, params={"work_date": "2026-11-02"}, headers=_headers(token)
        )
        by_requirement = client.get(
            PATH, params={"shift_requirement_id": "31"}, headers=_headers(token)
        )
        by_user = client.get(
            PATH, params={"user_id": str(LARGE_ID)}, headers=_headers(token)
        )
        combined = client.get(
            PATH,
            params={
                "work_date": "2026-11-02",
                "shift_requirement_id": "31",
                "user_id": "8",
            },
            headers=_headers(token),
        )
        foreign = client.get(
            PATH, params={"shift_requirement_id": "34"}, headers=_headers(token)
        )
        empty = client.get(
            "/api/v1/schedules/47/shift-assignments", headers=_headers(token)
        )
    assert page.json() == {
        "items": [by_requirement.json()["items"][1]],
        "limit": 1,
        "offset": 2,
        "total": 4,
    }
    assert max_limit.json()["limit"] == 100
    assert end.json()["items"] == [] and end.json()["total"] == 4
    assert [x["id"] for x in by_date.json()["items"]] == ["101", "102", "103"]
    assert [x["id"] for x in by_requirement.json()["items"]] == ["101", "102"]
    assert [x["id"] for x in by_user.json()["items"]] == ["101"]
    assert [x["id"] for x in combined.json()["items"]] == ["102"]
    assert (
        foreign.json()
        == empty.json()
        == {"items": [], "limit": 25, "offset": 0, "total": 0}
    )


@pytest.mark.parametrize(
    "status", ["draft", "collecting", "collection_closed", "closed", "published"]
)
def test_reads_in_every_status(assignment_api, status) -> None:
    app, token, _, engine = assignment_api
    with Session(engine) as session:
        session.get(Schedule, SCHEDULE_ID).status = status
        session.commit()
    with TestClient(app) as client:
        assert client.get(PATH, headers=_headers(token)).status_code == 200
        assert client.get(f"{PATH}/101", headers=_headers(token)).status_code == 200


@pytest.mark.parametrize(
    "path", [PATH, f"{PATH}/101", "/api/v1/schedules/999/shift-assignments"]
)
def test_employee_forbidden_before_lookup(assignment_api, path) -> None:
    app, token, actor, _ = assignment_api
    actor.role.code = "employee"
    with TestClient(app) as client:
        result = client.get(path, headers=_headers(token))
    assert result.status_code == 403
    assert result.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize("path", [PATH, f"{PATH}/101"])
def test_missing_or_inactive_identity_is_401(assignment_api, path) -> None:
    app, token, actor, _ = assignment_api
    with TestClient(app) as client:
        missing = client.get(path)
        actor.is_active = False
        inactive = client.get(path, headers=_headers(token))
    for response in (missing, inactive):
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/schedules/999/shift-assignments",
        "/api/v1/schedules/999/shift-assignments/101",
        f"{PATH}/999",
        f"{PATH}/104",
        f"{PATH}/0",
    ],
)
def test_missing_or_foreign_resources_are_404(assignment_api, path) -> None:
    app, token, _, _ = assignment_api
    with TestClient(app) as client:
        response = client.get(path, headers=_headers(token))
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["type"] == "about:blank"


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=101",
        "limit=1.5",
        "limit=true",
        "offset=-1",
        "offset=9223372036854775808",
        "work_date=2026-11-31",
        "work_date=2026-1-01",
        "shift_requirement_id=0",
        "user_id=abc",
        "user_id=9223372036854775808",
        "limit=1&limit=2",
        "sort=id",
        "unknown=x",
    ],
)
def test_invalid_query_is_422(assignment_api, query) -> None:
    app, token, _, _ = assignment_api
    with TestClient(app) as client:
        response = client.get(f"{PATH}?{query}", headers=_headers(token))
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert response.json()["errors"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/schedules/-1/shift-assignments",
        "/api/v1/schedules/9223372036854775808/shift-assignments",
        f"{PATH}/-1",
        f"{PATH}/9223372036854775808",
    ],
)
def test_invalid_path_is_422(assignment_api, path) -> None:
    app, token, _, _ = assignment_api
    with TestClient(app) as client:
        response = client.get(path, headers=_headers(token))
    assert response.status_code == 422
    assert response.json()["errors"]


def test_list_uses_bounded_queries_without_n_plus_one(assignment_api) -> None:
    app, token, _, engine = assignment_api
    statements = []

    def collect(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", collect)
    try:
        with TestClient(app) as client:
            assert client.get(PATH, headers=_headers(token)).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", collect)
    assert len(statements) == 3  # schedule, filtered count, joined page


def test_openapi_has_only_read_routes_and_public_schema(assignment_api) -> None:
    app, _, _, _ = assignment_api
    paths = app.openapi()["paths"]
    list_path = "/api/v1/schedules/{schedule_id}/shift-assignments"
    detail_path = f"{list_path}/{{assignment_id}}"
    assert set(paths[list_path]) == {"get", "post"}
    assert set(paths[detail_path]) == {"get", "delete"}
    for path in (list_path, detail_path):
        operation = paths[path]["get"]
        assert operation["security"] == [{"BearerAuth": []}]
        assert {"200", "401", "403", "404", "422"} <= set(operation["responses"])
    assert {
        p["name"] for p in paths[list_path]["get"]["parameters"] if p["in"] == "query"
    } == {"limit", "offset", "work_date", "shift_requirement_id", "user_id"}
    schema = app.openapi()["components"]["schemas"]["ShiftAssignmentResponse"]
    assert set(schema["properties"]) == FIELDS
