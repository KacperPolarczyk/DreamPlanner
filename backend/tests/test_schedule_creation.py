"""HTTP and transaction contract checks for monthly draft creation."""

import base64
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.auth import session_service
from src.core.database import Base, get_session
from src.main import create_app
from src.schedules.models import Schedule
from src.schedules.service import _period_conflict

PATH = "/api/v1/schedules"
CREATOR_ID = 9007199254740993
FIRST_ID = 9007199254740994


@pytest.fixture
def creation_api(monkeypatch: pytest.MonkeyPatch):
    """Use disposable SQLite storage and the real Bearer role dependency."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "schedule-creation-test-v1")
    monkeypatch.setenv("JWT_ISSUER", "dreamplanner-schedule-creation-test")
    monkeypatch.setenv("JWT_AUDIENCE", "dreamplanner-schedule-creation-api")
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[Schedule.__table__])
    next_id = FIRST_ID

    def assign_sqlite_identity(mapper, connection, schedule) -> None:
        """Simulate PostgreSQL BIGINT identity for SQLite HTTP tests."""
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
        """Supply current identity for Bearer validation."""

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


def _rows(engine) -> list[Schedule]:
    with Session(engine) as session:
        return list(session.scalars(select(Schedule).order_by(Schedule.id)))


def test_create_empty_draft_returns_full_response_and_persists(creation_api) -> None:
    app, token, _, engine = creation_api
    before = datetime.now(UTC) - timedelta(seconds=2)
    with TestClient(app) as client:
        response = client.post(
            PATH, json={"year": 2027, "month": 1}, headers=_headers(token)
        )
    after = datetime.now(UTC) + timedelta(seconds=2)
    assert response.status_code == 201
    assert response.headers["location"] == f"{PATH}/{FIRST_ID}"
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload == {
        "id": str(FIRST_ID),
        "year": 2027,
        "month": 1,
        "status": "draft",
        "created_by": str(CREATOR_ID),
        "created_at": payload["created_at"],
        "collection_opened_at": None,
        "collection_closed_at": None,
        "published_at": None,
    }
    assert isinstance(payload["id"], str)
    assert isinstance(payload["created_by"], str)
    assert before <= datetime.fromisoformat(payload["created_at"]) <= after
    rows = _rows(engine)
    assert len(rows) == 1
    assert (rows[0].year, rows[0].month, rows[0].created_by) == (2027, 1, CREATOR_ID)
    assert rows[0].status == "draft"


@pytest.mark.parametrize("year,month", [(2000, 1), (2026, 10), (2100, 12)])
def test_past_current_future_and_boundaries(creation_api, year, month) -> None:
    app, token, _, _ = creation_api
    with TestClient(app) as client:
        response = client.post(
            PATH, json={"year": year, "month": month}, headers=_headers(token)
        )
    assert response.status_code == 201
    assert (response.json()["year"], response.json()["month"]) == (year, month)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"year": 2027},
        {"month": 1},
        {"year": 1999, "month": 1},
        {"year": 2101, "month": 1},
        {"year": 2027, "month": 0},
        {"year": 2027, "month": 13},
        {"year": True, "month": 1},
        {"year": 2027, "month": False},
        {"year": "2027", "month": 1},
        {"year": 2027.0, "month": 1},
        {"year": None, "month": 1},
        {"year": 2027, "month": "1"},
        {"year": 2027, "month": None},
        {"year": 2027, "month": 1, "status": "published"},
        {"year": 2027, "month": 1, "created_by": "1"},
        {"year": 2027, "month": 1, "created_at": "2027-01-01T00:00:00Z"},
        {"year": 2027, "month": 1, "published_at": None},
        {"year": 2027, "month": 1, "id": "1"},
    ],
)
def test_invalid_request_is_422(creation_api, payload) -> None:
    app, token, _, engine = creation_api
    with TestClient(app) as client:
        response = client.post(PATH, json=payload, headers=_headers(token))
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert response.json()["errors"]
    assert _rows(engine) == []


def test_conflict_is_safe_and_following_write_succeeds(creation_api) -> None:
    app, token, _, engine = creation_api
    with TestClient(app) as client:
        first = client.post(
            PATH, json={"year": 2027, "month": 1}, headers=_headers(token)
        )
        conflict = client.post(
            PATH, json={"year": 2027, "month": 1}, headers=_headers(token)
        )
        second = client.post(
            PATH, json={"year": 2027, "month": 2}, headers=_headers(token)
        )
    assert [r.status_code for r in (first, conflict, second)] == [201, 409, 201]
    assert conflict.headers["content-type"] == "application/problem+json"
    assert conflict.headers["cache-control"] == "no-store"
    assert conflict.json() == {
        "type": "urn:dreamplanner:problem:schedule-period-conflict",
        "title": "Okres grafiku jest już zajęty",
        "status": 409,
        "detail": "Grafik dla tego roku i miesiąca już istnieje.",
    }
    assert "UNIQUE" not in conflict.text
    assert "uq_schedules_period" not in conflict.text
    assert [(r.year, r.month) for r in _rows(engine)] == [(2027, 1), (2027, 2)]


@pytest.mark.parametrize("role", ["admin", "employee"])
def test_other_roles_cannot_create(creation_api, role) -> None:
    app, token, actor, engine = creation_api
    actor.role.code = role
    with TestClient(app) as client:
        response = client.post(
            PATH, json={"year": 2027, "month": 1}, headers=_headers(token)
        )
    assert response.status_code == 403
    assert _rows(engine) == []


def test_no_token_cannot_create(creation_api) -> None:
    app, _, _, engine = creation_api
    with TestClient(app) as client:
        response = client.post(PATH, json={"year": 2027, "month": 1})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert _rows(engine) == []


def test_invalid_json_returns_400_problem(creation_api) -> None:
    app, token, _, engine = creation_api
    with TestClient(app) as client:
        response = client.post(
            PATH,
            content='{"year":',
            headers={**_headers(token), "Content-Type": "application/json"},
        )
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    assert _rows(engine) == []


def test_only_period_constraint_maps_to_conflict() -> None:
    class PostgresError(Exception):
        sqlstate = "23505"

        def __init__(self, constraint_name: str) -> None:
            self.diag = SimpleNamespace(constraint_name=constraint_name)

    expected = IntegrityError("INSERT", {}, PostgresError("uq_schedules_period"))
    other = IntegrityError("INSERT", {}, PostgresError("schedules_pkey"))
    assert _period_conflict(expected)
    assert not _period_conflict(other)


def test_openapi_documents_creation_contract(creation_api) -> None:
    app, _, _, _ = creation_api
    operation = app.openapi()["paths"][PATH]["post"]
    assert operation["security"] == [{"BearerAuth": []}]
    assert "`manager`" in operation["description"]
    assert "Location" in operation["description"]
    assert {"201", "400", "401", "403", "409", "422"} <= set(operation["responses"])
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CreateScheduleInput"
    }
    assert operation["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ScheduleResponse"
    }
    for status in ("400", "401", "403", "409", "422"):
        assert "application/problem+json" in operation["responses"][status]["content"]
    schema = app.openapi()["components"]["schemas"]["CreateScheduleInput"]
    assert set(schema["required"]) == {"year", "month"}
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"year", "month"}
