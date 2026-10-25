"""HTTP contract checks for the manager employee directory."""

import base64
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from src.auth import session_service
from src.core.database import get_session
from src.main import create_app
from src.users import employee_router

PATH = "/api/v1/employees"
NOW = datetime(2027, 1, 5, 12, tzinfo=UTC)


@pytest.fixture
def employee_api(monkeypatch: pytest.MonkeyPatch):
    """Exercise real JWT and role dependencies with controlled database reads."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "employee-reads-test-v1")
    monkeypatch.setenv("JWT_ISSUER", "dreamplanner-employee-reads-test")
    monkeypatch.setenv("JWT_AUDIENCE", "dreamplanner-employee-reads-api")
    app = create_app()
    app.dependency_overrides[get_session] = lambda: object()
    session_id = uuid4()
    actor = SimpleNamespace(
        is_active=True, activated_at=NOW, role=SimpleNamespace(code="manager")
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
    calls = []

    class EmployeesRepository:
        def __init__(self, session):
            pass

        def list_eligible_employees(self, **kwargs):
            calls.append(kwargs)
            return [(9007199254740993, "Anna Nowak")], 1

    monkeypatch.setattr(employee_router, "UserRepository", EmployeesRepository)
    token = app.state.jwt_codec.issue(42, session_id)
    return app, token, actor, auth_session, calls


def test_manager_and_admin_receive_minimal_page(employee_api) -> None:
    app, token, actor, _, calls = employee_api
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        manager = client.get(PATH, headers=headers)
        actor.role.code = "admin"
        admin = client.get(
            PATH, params={"q": "Anna", "limit": 1, "offset": 3}, headers=headers
        )
    for response in (manager, admin):
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["items"] == [
            {"user_id": "9007199254740993", "full_name": "Anna Nowak"}
        ]
        assert set(response.json()) == {"items", "limit", "offset", "total"}
    assert calls == [
        {"query": None, "limit": 25, "offset": 0},
        {"query": "Anna", "limit": 1, "offset": 3},
    ]


def test_current_role_and_account_state_control_access(employee_api) -> None:
    app, token, actor, auth_session, calls = employee_api
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        actor.role.code = "employee"
        forbidden = client.get(PATH, headers=headers)
        actor.role.code = "manager"
        allowed = client.get(PATH, headers=headers)
        actor.is_active = False
        inactive = client.get(PATH, headers=headers)
        actor.is_active = True
        actor.activated_at = None
        pending = client.get(PATH, headers=headers)
        actor.activated_at = NOW
        auth_session.revoked_at = NOW
        revoked = client.get(PATH, headers=headers)
    assert forbidden.status_code == 403
    assert allowed.status_code == 200
    assert [r.status_code for r in (inactive, pending, revoked)] == [401] * 3
    assert len(calls) == 1
    for response in (forbidden, inactive, pending, revoked):
        assert response.headers["content-type"] == "application/problem+json"
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["type"] == "about:blank"
    for response in (inactive, pending, revoked):
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("authorization", [None, "Bearer invalid"])
def test_missing_or_invalid_bearer(employee_api, authorization) -> None:
    app, _, _, _, calls = employee_api
    headers = {"Authorization": authorization} if authorization else {}
    with TestClient(app) as client:
        response = client.get(PATH, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"
    assert calls == []


@pytest.mark.parametrize(
    "query",
    [
        "?unknown=1",
        "?q=a&q=b",
        "?limit=1&limit=2",
        "?offset=0&offset=1",
        "?limit=0",
        "?limit=101",
        "?offset=-1",
        "?offset=9223372036854775808",
    ],
)
def test_invalid_query_is_validation_problem(employee_api, query) -> None:
    app, token, _, _, calls = employee_api
    with TestClient(app) as client:
        response = client.get(
            PATH + query, headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert response.json()["errors"]
    assert calls == []


def test_empty_query_and_pagination_boundaries(employee_api) -> None:
    app, token, _, _, calls = employee_api
    with TestClient(app) as client:
        response = client.get(
            PATH,
            params={"q": "", "limit": 100, "offset": 9223372036854775807},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert calls == [{"query": "", "limit": 100, "offset": 9223372036854775807}]


def test_openapi_documents_directory_contract(employee_api) -> None:
    app, _, _, _, _ = employee_api
    operation = app.openapi()["paths"][PATH]["get"]
    assert operation["security"] == [{"BearerAuth": []}]
    assert "/api/v1/employees/{user_id}" not in app.openapi()["paths"]
    parameters = {item["name"]: item["schema"] for item in operation["parameters"]}
    assert set(parameters) == {"q", "limit", "offset"}
    assert parameters["limit"]["default"] == 25
    assert parameters["limit"]["minimum"] == 1
    assert parameters["limit"]["maximum"] == 100
    assert parameters["offset"]["default"] == 0
    assert parameters["offset"]["maximum"] == 9223372036854775807
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/EmployeeListResponse"
    }
    for status in ("200", "401", "403", "422"):
        assert (
            operation["responses"][status]["headers"]["Cache-Control"]["schema"][
                "const"
            ]
            == "no-store"
        )
    for status in ("401", "403", "422"):
        assert "application/problem+json" in operation["responses"][status]["content"]
    assert (
        operation["responses"]["401"]["headers"]["WWW-Authenticate"]["schema"]["const"]
        == "Bearer"
    )
