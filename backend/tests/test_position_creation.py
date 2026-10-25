"""HTTP and transaction checks for administrator position creation."""

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
from src.positions.models import Position
from src.positions.service import _name_conflict

PATH = "/api/v1/positions"
LARGE_ID = 9007199254740993


@pytest.fixture
def create_api(monkeypatch: pytest.MonkeyPatch):
    """Use disposable SQLite rows and the real Bearer role dependency."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "position-creation-test-v1")
    monkeypatch.setenv("JWT_ISSUER", "dreamplanner-position-creation-test")
    monkeypatch.setenv("JWT_AUDIENCE", "dreamplanner-position-creation-api")
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def register_btrim(connection, record) -> None:
        """Support the PostgreSQL whitespace check in SQLite."""
        connection.create_function("btrim", 1, str.strip)

    Base.metadata.create_all(engine, tables=[Position.__table__])
    with Session(engine) as session:
        session.add_all(
            [
                Position(id=10, name="Kitchen", is_active=True),
                Position(id=11, name="Reception", is_active=False),
            ]
        )
        session.commit()

    next_id = LARGE_ID

    def assign_sqlite_identity(mapper, connection, position) -> None:
        """Simulate PostgreSQL BIGINT identity for SQLite-only HTTP tests."""
        nonlocal next_id
        if position.id is None:
            position.id = next_id
            next_id += 1

    event.listen(Position, "before_insert", assign_sqlite_identity)
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
        """Provide a current database identity for Bearer validation."""

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
    yield app, token, actor, engine
    app.dependency_overrides.clear()
    event.remove(Position, "before_insert", assign_sqlite_identity)
    engine.dispose()


def _headers(token: str) -> dict[str, str]:
    """Build a valid Bearer header for a position request."""
    return {"Authorization": f"Bearer {token}"}


def _positions(engine) -> list[Position]:
    """Read committed rows from a separate test session."""
    with Session(engine) as session:
        return list(session.scalars(select(Position).order_by(Position.id)))


def test_admin_creates_and_persists_normalized_position(create_api) -> None:
    app, token, _, engine = create_api
    with TestClient(app) as client:
        response = client.post(
            PATH, json={"name": "  Barista  "}, headers=_headers(token)
        )
    assert response.status_code == 201
    assert response.headers["location"] == f"{PATH}/{LARGE_ID}"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "id": str(LARGE_ID),
        "name": "Barista",
        "is_active": True,
    }
    assert isinstance(response.json()["id"], str)
    assert [(row.id, row.name, row.is_active) for row in _positions(engine)] == [
        (10, "Kitchen", True),
        (11, "Reception", False),
        (LARGE_ID, "Barista", True),
    ]


@pytest.mark.parametrize("role", ["manager", "employee"])
def test_non_admin_roles_cannot_create(create_api, role) -> None:
    app, token, actor, engine = create_api
    actor.role.code = role
    with TestClient(app) as client:
        response = client.post(PATH, json={"name": "Barista"}, headers=_headers(token))
    assert response.status_code == 403
    assert response.headers["content-type"] == "application/problem+json"
    assert len(_positions(engine)) == 2


def test_missing_bearer_cannot_create(create_api) -> None:
    app, _, _, engine = create_api
    with TestClient(app) as client:
        response = client.post(PATH, json={"name": "Barista"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert len(_positions(engine)) == 2


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 121},
        {"name": None},
        {"name": 5},
        {"name": ["Barista"]},
        {"name": "Barista", "is_active": False},
        {"name": "Barista", "id": "99"},
        {"name": "Bar\nista"},
    ],
)
def test_invalid_body_returns_safe_422(create_api, payload) -> None:
    app, token, _, engine = create_api
    with TestClient(app) as client:
        response = client.post(PATH, json=payload, headers=_headers(token))
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert response.json()["status"] == 422
    assert response.json()["errors"]
    assert len(_positions(engine)) == 2


def test_name_of_exactly_120_characters_is_accepted(create_api) -> None:
    app, token, _, engine = create_api
    with TestClient(app) as client:
        response = client.post(PATH, json={"name": "A" * 120}, headers=_headers(token))
    assert response.status_code == 201
    assert response.json()["name"] == "A" * 120
    assert len(_positions(engine)) == 3


@pytest.mark.parametrize("name", ["Kitchen", "  Kitchen  ", "Reception"])
def test_duplicate_active_or_inactive_name_returns_409(create_api, name) -> None:
    app, token, _, engine = create_api
    with TestClient(app) as client:
        response = client.post(PATH, json={"name": name}, headers=_headers(token))
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "urn:dreamplanner:problem:position-name-conflict",
        "title": "Nazwa stanowiska jest już zajęta",
        "status": 409,
        "detail": "Stanowisko o tej nazwie już istnieje.",
    }
    assert "UNIQUE" not in response.text
    assert len(_positions(engine)) == 2


def test_case_differences_remain_distinct(create_api) -> None:
    app, token, _, engine = create_api
    with TestClient(app) as client:
        response = client.post(PATH, json={"name": "kitchen"}, headers=_headers(token))
    assert response.status_code == 201
    assert response.json()["name"] == "kitchen"
    assert [row.name for row in _positions(engine)] == [
        "Kitchen",
        "Reception",
        "kitchen",
    ]


def test_invalid_json_is_400_problem(create_api) -> None:
    app, token, _, engine = create_api
    with TestClient(app) as client:
        response = client.post(
            PATH,
            content='{"name":',
            headers={**_headers(token), "Content-Type": "application/json"},
        )
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    assert len(_positions(engine)) == 2


def test_only_position_name_unique_violation_is_a_name_conflict() -> None:
    class PostgresError(Exception):
        """Mimic the diagnostic fields of a PostgreSQL DBAPI exception."""

        sqlstate = "23505"

        def __init__(self, constraint_name: str) -> None:
            self.diag = SimpleNamespace(constraint_name=constraint_name)

    expected = IntegrityError("INSERT", {}, PostgresError("positions_name_key"))
    other = IntegrityError("INSERT", {}, PostgresError("positions_pkey"))
    assert _name_conflict(expected)
    assert not _name_conflict(other)


def test_openapi_documents_admin_creation_and_conflict(create_api) -> None:
    app, _, _, _ = create_api
    operation = app.openapi()["paths"][PATH]["post"]
    assert operation["security"] == [{"BearerAuth": []}]
    assert "`admin`" in operation["description"]
    assert {"201", "400", "401", "403", "409", "422"} <= set(operation["responses"])
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CreatePositionInput"
    }
    assert operation["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PositionResponse"
    }
    for status in ("400", "401", "403", "409", "422"):
        assert "application/problem+json" in operation["responses"][status]["content"]
