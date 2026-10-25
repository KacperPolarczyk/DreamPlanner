"""HTTP contract checks for administrator account reads."""

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
from src.users import router as users_router

PATH = "/api/v1/users"
NOW = datetime(2027, 1, 5, 12, tzinfo=UTC)


@pytest.fixture
def read_api(monkeypatch: pytest.MonkeyPatch):
    """Authenticate real Bearer requests with controlled account reads."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "user-reads-test-v1")
    monkeypatch.setenv("JWT_ISSUER", "dreamplanner-user-reads-test")
    monkeypatch.setenv("JWT_AUDIENCE", "dreamplanner-user-reads-api")
    app = create_app()
    session_id = uuid4()
    actor = SimpleNamespace(
        id=42, is_active=True, activated_at=NOW, role=SimpleNamespace(code="admin")
    )
    auth_session = SimpleNamespace(
        user_id=42,
        revoked_at=None,
        idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
        absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    users = [
        SimpleNamespace(
            id=9007199254740993 + i,
            email=f"person{i}@example.com",
            full_name=f"Person {i}",
            role=SimpleNamespace(code="employee" if i else "manager"),
            is_active=bool(i),
            activated_at=NOW if i else None,
            created_at=NOW,
            updated_at=NOW,
            password_hash="hidden-password-hash",
            refresh_token="hidden-refresh-token",
            token_hash="hidden-token-hash",
        )
        for i in range(2)
    ]
    app.dependency_overrides[get_session] = lambda: object()

    class AuthRepository:
        """Supply current database identity to the shared auth dependency."""

        def get_session(self, requested_id):
            return auth_session if requested_id == session_id else None

        def get_user_fresh(self, requested_id):
            return actor if requested_id == 42 else None

    monkeypatch.setattr(
        session_service, "AuthSessionRepository", lambda session: AuthRepository()
    )
    calls = []

    class Repository:
        """Record authorized reads while returning public test accounts."""

        def __init__(self, session):
            pass

        def list_users(self, **kwargs):
            calls.append(("list", kwargs))
            return users, 2

        def get_by_id(self, user_id):
            calls.append(("detail", user_id))
            return next((user for user in users if user.id == user_id), None)

    monkeypatch.setattr(users_router, "UserRepository", Repository)
    token = app.state.jwt_codec.issue(42, session_id)
    return app, token, actor, users, calls


def test_admin_list_contains_active_and_inactive_accounts_without_secrets(
    read_api,
) -> None:
    app, token, _, users, calls = read_api
    with TestClient(app) as client:
        response = client.get(PATH, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert (payload["limit"], payload["offset"], payload["total"]) == (25, 0, 2)
    assert [item["id"] for item in payload["items"]] == [str(user.id) for user in users]
    assert [item["is_active"] for item in payload["items"]] == [False, True]
    assert payload["items"][0]["activated_at"] is None
    assert set(payload["items"][0]) == {
        "id",
        "email",
        "full_name",
        "role",
        "is_active",
        "activated_at",
        "created_at",
        "updated_at",
    }
    assert "hidden-" not in response.text
    assert calls[0][1] == {
        "role": None,
        "is_active": None,
        "activated": None,
        "query": None,
        "limit": 25,
        "offset": 0,
        "sort": "full_name",
    }


def test_admin_list_passes_approved_filters_and_pagination(read_api) -> None:
    app, token, _, _, calls = read_api
    with TestClient(app) as client:
        response = client.get(
            PATH,
            params={
                "role": "employee",
                "is_active": "false",
                "activated": "true",
                "q": "PERSON",
                "limit": "100",
                "offset": "7",
                "sort": "-created_at",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert calls == [
        (
            "list",
            {
                "role": "employee",
                "is_active": False,
                "activated": True,
                "query": "PERSON",
                "limit": 100,
                "offset": 7,
                "sort": "-created_at",
            },
        )
    ]


def test_admin_detail_and_missing_account(read_api) -> None:
    app, token, _, users, calls = read_api
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        found = client.get(f"{PATH}/{users[0].id}", headers=headers)
        missing = client.get(f"{PATH}/999", headers=headers)
    assert found.status_code == 200
    assert found.json()["id"] == str(users[0].id)
    assert found.json()["is_active"] is False
    assert "hidden-" not in found.text
    assert found.headers["cache-control"] == "no-store"
    assert missing.status_code == 404
    assert missing.headers["content-type"] == "application/problem+json"
    assert missing.headers["cache-control"] == "no-store"
    assert calls == [("detail", users[0].id), ("detail", 999)]


@pytest.mark.parametrize("role", ["manager", "employee"])
def test_other_roles_cannot_read_accounts(read_api, role) -> None:
    app, token, actor, users, calls = read_api
    actor.role.code = role
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        responses = [
            client.get(PATH, headers=headers),
            client.get(f"{PATH}/{users[0].id}", headers=headers),
        ]
    assert [response.status_code for response in responses] == [403, 403]
    assert all(
        r.headers["content-type"] == "application/problem+json" for r in responses
    )
    assert calls == []


@pytest.mark.parametrize("authorization", [None, "Bearer invalid"])
def test_account_reads_require_bearer(read_api, authorization) -> None:
    app, _, _, users, calls = read_api
    headers = {"Authorization": authorization} if authorization else {}
    with TestClient(app) as client:
        responses = [
            client.get(PATH, headers=headers),
            client.get(f"{PATH}/{users[0].id}", headers=headers),
        ]
    assert [response.status_code for response in responses] == [401, 401]
    assert all(r.headers["www-authenticate"] == "Bearer" for r in responses)
    assert calls == []


@pytest.mark.parametrize(
    ("path", "params"),
    [
        (PATH, {"unexpected": "yes"}),
        (PATH, {"role": "owner"}),
        (PATH, {"is_active": "maybe"}),
        (PATH, {"sort": "id"}),
        (PATH, {"limit": "0"}),
        (PATH, {"limit": "101"}),
        (PATH, {"offset": "-1"}),
        (PATH, {"offset": "9223372036854775808"}),
        (f"{PATH}/invalid", None),
        (f"{PATH}/-1", None),
        (f"{PATH}/9223372036854775808", None),
    ],
)
def test_invalid_read_parameters_return_validation_problem(
    read_api, path, params
) -> None:
    app, token, _, _, calls = read_api
    with TestClient(app) as client:
        response = client.get(
            path, params=params, headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert calls == []


def test_static_me_route_and_openapi_security(read_api) -> None:
    app, _, _, _, _ = read_api
    paths = app.openapi()["paths"]
    assert "/api/v1/users/me" in paths
    for path in (PATH, f"{PATH}/{{user_id}}"):
        operation = paths[path]["get"]
        assert operation["security"] == [{"BearerAuth": []}]
        for status in ("401", "403", "422"):
            assert (
                "application/problem+json" in operation["responses"][status]["content"]
            )
    assert paths[PATH]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/UserListResponse"}
