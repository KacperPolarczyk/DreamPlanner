"""HTTP and query contract checks for position reads."""

import base64
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.auth import session_service
from src.core.database import Base, get_session
from src.main import create_app
from src.positions.models import Position

PATH = "/api/v1/positions"
LARGE_ID = 9007199254740993


@pytest.fixture
def positions_api(monkeypatch: pytest.MonkeyPatch):
    """Use a disposable database with real position queries and Bearer checks."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "position-reads-test-v1")
    monkeypatch.setenv("JWT_ISSUER", "dreamplanner-position-reads-test")
    monkeypatch.setenv("JWT_AUDIENCE", "dreamplanner-position-reads-api")
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def register_btrim(connection, record) -> None:
        """Support the position model's PostgreSQL check in SQLite."""
        connection.create_function("btrim", 1, str.strip)

    Base.metadata.create_all(engine, tables=[Position.__table__])
    with Session(engine) as session:
        session.add_all(
            [
                Position(id=10, name="Bar", is_active=True),
                Position(id=11, name="Kitchen", is_active=False),
                Position(id=12, name="Reception", is_active=True),
                Position(id=LARGE_ID, name="Literal%_", is_active=False),
            ]
        )
        session.commit()

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
        """Supply current identity for real role dependencies."""

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
    yield app, token, actor
    app.dependency_overrides.clear()
    engine.dispose()


def _headers(token: str) -> dict[str, str]:
    """Build a valid Bearer header for protected reads."""
    return {"Authorization": f"Bearer {token}"}


def _ids(response) -> list[str]:
    """Return the public IDs in a position page."""
    return [item["id"] for item in response.json()["items"]]


@pytest.mark.parametrize("role", ["admin", "manager"])
def test_list_and_detail_show_inactive_positions(positions_api, role) -> None:
    app, token, actor = positions_api
    actor.role.code = role
    with TestClient(app) as client:
        listing = client.get(PATH, headers=_headers(token))
        detail = client.get(f"{PATH}/{LARGE_ID}", headers=_headers(token))
    assert listing.status_code == detail.status_code == 200
    assert listing.json()["total"] == 4
    assert listing.json()["limit"] == 25
    assert listing.json()["offset"] == 0
    assert listing.json()["items"][1]["is_active"] is False
    assert detail.json() == {
        "id": str(LARGE_ID),
        "name": "Literal%_",
        "is_active": False,
    }
    assert isinstance(detail.json()["id"], str)
    assert listing.headers["cache-control"] == "no-store"
    assert detail.headers["cache-control"] == "no-store"


def test_employee_and_unauthenticated_callers_cannot_read(positions_api) -> None:
    app, token, actor = positions_api
    actor.role.code = "employee"
    with TestClient(app) as client:
        forbidden = [
            client.get(PATH, headers=_headers(token)),
            client.get(f"{PATH}/999", headers=_headers(token)),
        ]
        unauthenticated = [client.get(PATH), client.get(f"{PATH}/10")]
    assert [response.status_code for response in forbidden] == [403, 403]
    assert [response.status_code for response in unauthenticated] == [401, 401]
    assert all(
        response.headers["content-type"] == "application/problem+json"
        for response in forbidden + unauthenticated
    )
    assert all(
        response.headers["www-authenticate"] == "Bearer" for response in unauthenticated
    )


def test_pagination_filters_and_literal_search(positions_api) -> None:
    app, token, _ = positions_api
    with TestClient(app) as client:
        page = client.get(
            PATH, params={"limit": 1, "offset": 1}, headers=_headers(token)
        )
        empty = client.get(
            PATH, params={"limit": 1, "offset": 99}, headers=_headers(token)
        )
        active = client.get(PATH, params={"is_active": "true"}, headers=_headers(token))
        inactive = client.get(
            PATH, params={"is_active": "false"}, headers=_headers(token)
        )
        search = client.get(PATH, params={"q": "  KITCH  "}, headers=_headers(token))
        literal = client.get(PATH, params={"q": "%_"}, headers=_headers(token))
    assert _ids(page) == ["11"]
    assert page.json()["total"] == 4
    assert empty.json()["items"] == []
    assert empty.json()["total"] == 4
    assert _ids(active) == ["10", "12"]
    assert _ids(inactive) == ["11", str(LARGE_ID)]
    assert _ids(search) == ["11"]
    assert _ids(literal) == [str(LARGE_ID)]


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("name", ["10", "11", str(LARGE_ID), "12"]),
        ("-name", ["12", str(LARGE_ID), "11", "10"]),
        ("id", ["10", "11", "12", str(LARGE_ID)]),
        ("-id", [str(LARGE_ID), "12", "11", "10"]),
    ],
)
def test_supported_sort_orders(positions_api, sort, expected) -> None:
    app, token, _ = positions_api
    with TestClient(app) as client:
        response = client.get(PATH, params={"sort": sort}, headers=_headers(token))
    assert response.status_code == 200
    assert _ids(response) == expected


@pytest.mark.parametrize(
    ("path", "params"),
    [
        (PATH, {"unexpected": "yes"}),
        (PATH, {"is_active": "maybe"}),
        (PATH, {"q": ""}),
        (PATH, {"q": "  "}),
        (PATH, {"q": "x" * 121}),
        (PATH, {"sort": "unknown"}),
        (PATH, {"limit": "0"}),
        (PATH, {"limit": "101"}),
        (PATH, {"offset": "-1"}),
        (PATH, {"offset": "9223372036854775808"}),
        (f"{PATH}/invalid", None),
        (f"{PATH}/-1", None),
        (f"{PATH}/9223372036854775808", None),
    ],
)
def test_invalid_inputs_return_422_problem(positions_api, path, params) -> None:
    app, token, _ = positions_api
    with TestClient(app) as client:
        response = client.get(path, params=params, headers=_headers(token))
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert response.json()["errors"]


def test_missing_position_returns_404_and_zero_is_not_found(positions_api) -> None:
    app, token, _ = positions_api
    with TestClient(app) as client:
        missing = client.get(f"{PATH}/999", headers=_headers(token))
        zero = client.get(f"{PATH}/0", headers=_headers(token))
        leading_zero = client.get(f"{PATH}/0010", headers=_headers(token))
    assert missing.status_code == zero.status_code == 404
    assert missing.headers["content-type"] == "application/problem+json"
    assert missing.json()["type"] == "about:blank"
    assert leading_zero.status_code == 200
    assert leading_zero.json()["id"] == "10"


def test_openapi_documents_only_two_secured_reads(positions_api) -> None:
    app, _, _ = positions_api
    paths = app.openapi()["paths"]
    assert "post" not in paths[PATH]
    for path, statuses, result in (
        (PATH, {"200", "401", "403", "422"}, "PositionListResponse"),
        (
            f"{PATH}/{{position_id}}",
            {"200", "401", "403", "404", "422"},
            "PositionResponse",
        ),
    ):
        operation = paths[path]["get"]
        assert set(paths[path]) == {"get"}
        assert operation["security"] == [{"BearerAuth": []}]
        assert "`admin`" in operation["description"]
        assert "`manager`" in operation["description"]
        assert statuses <= set(operation["responses"])
        assert operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ] == {"$ref": f"#/components/schemas/{result}"}
        for status in statuses - {"200"}:
            assert (
                "application/problem+json" in operation["responses"][status]["content"]
            )
    public = paths[PATH]["get"]
    assert {"is_active", "q", "limit", "offset", "sort"} <= {
        parameter["name"] for parameter in public["parameters"]
    }
