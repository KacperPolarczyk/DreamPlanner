"""HTTP contract checks for global logout and own-password change."""

import base64
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from src.auth import router as auth_router
from src.auth import session_service
from src.core.database import get_session
from src.main import create_app
from src.users import router as users_router
from src.users.password_service import InvalidCurrentPassword

LOGOUT = "/api/v1/auth/sessions"
CHANGE = "/api/v1/users/me/password-change"
ORIGIN = "http://localhost:5173"


@pytest.fixture
def browser_app(monkeypatch: pytest.MonkeyPatch):
    """Use signed JWTs and real auth dependencies with controlled commands."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "password-http-test-v1")
    monkeypatch.setenv("JWT_ISSUER", "dreamplanner-password-http-test")
    monkeypatch.setenv("JWT_AUDIENCE", "dreamplanner-password-http-api")
    app = create_app()
    session_id = uuid4()
    actor = SimpleNamespace(
        id=42,
        is_active=True,
        activated_at=datetime.now(UTC),
        role=SimpleNamespace(code="employee"),
    )
    current_session = SimpleNamespace(
        user_id=42,
        revoked_at=None,
        idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
        absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    app.dependency_overrides[get_session] = lambda: object()

    class AuthRepository:
        """Return mutable current account and session state for JWT checks."""

        def get_session(self, requested_id):
            return current_session if requested_id == session_id else None

        def get_user_fresh(self, requested_id):
            return actor if requested_id == 42 else None

    monkeypatch.setattr(
        session_service, "AuthSessionRepository", lambda session: AuthRepository()
    )
    calls = []

    def revoke(session, user_id, requested_session_id):
        calls.append(("logout", user_id, requested_session_id))
        current_session.revoked_at = datetime.now(UTC)

    def change(session, user_id, requested_session_id, current, new):
        calls.append(("password", user_id, requested_session_id))
        if current != "old-password":
            raise InvalidCurrentPassword()
        current_session.revoked_at = datetime.now(UTC)

    monkeypatch.setattr(auth_router, "revoke_all_sessions", revoke)
    monkeypatch.setattr(users_router, "change_own_password", change)
    token = app.state.jwt_codec.issue(42, session_id)
    return app, token, actor, current_session, calls


@pytest.mark.parametrize("role", ["admin", "manager", "employee"])
def test_global_logout_each_role_clears_cookie_and_blocks_access(
    browser_app, role
) -> None:
    app, token, actor, _, calls = browser_app
    actor.role.code = role
    headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}
    with TestClient(app) as client:
        response = client.delete(LOGOUT, headers=headers)
        reuse = client.get("/api/v1/users/me", headers=headers)
    assert response.status_code == 204
    assert response.text == ""
    assert response.headers["cache-control"] == "no-store"
    assert "Path=/api/v1/auth/sessions/current" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert reuse.status_code == 401
    assert reuse.headers["www-authenticate"] == "Bearer"
    assert calls == [("logout", 42, app.state.jwt_codec.verify(token)[1])]


@pytest.mark.parametrize("role", ["admin", "manager", "employee"])
def test_password_change_each_role_ends_current_session(browser_app, role) -> None:
    app, token, actor, _, calls = browser_app
    actor.role.code = role
    headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}
    with TestClient(app) as client:
        response = client.post(
            CHANGE,
            headers=headers,
            json={"current_password": "old-password", "new_password": "new-password"},
        )
        reuse = client.get("/api/v1/users/me", headers=headers)
    assert response.status_code == 204
    assert response.text == ""
    assert response.headers["cache-control"] == "no-store"
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert "old-password" not in response.text
    assert "new-password" not in response.text
    assert reuse.status_code == 401
    assert calls == [("password", 42, app.state.jwt_codec.verify(token)[1])]


@pytest.mark.parametrize("authorization", [None, "Bearer invalid"])
def test_commands_require_valid_bearer(browser_app, authorization) -> None:
    app, _, _, _, calls = browser_app
    headers = {"Origin": ORIGIN}
    if authorization is not None:
        headers["Authorization"] = authorization
    with TestClient(app) as client:
        responses = [
            client.delete(LOGOUT, headers=headers),
            client.post(
                CHANGE,
                headers=headers,
                json={
                    "current_password": "old-password",
                    "new_password": "new-password",
                },
            ),
        ]
    assert [response.status_code for response in responses] == [401, 401]
    assert all(r.headers["www-authenticate"] == "Bearer" for r in responses)
    assert all(
        r.headers["content-type"] == "application/problem+json" for r in responses
    )
    assert calls == []


@pytest.mark.parametrize("origin", [None, "https://evil.example"])
def test_global_logout_rejects_missing_or_foreign_origin(browser_app, origin) -> None:
    app, token, _, _, calls = browser_app
    headers = {"Authorization": f"Bearer {token}"}
    if origin is not None:
        headers["Origin"] = origin
    with TestClient(app) as client:
        response = client.delete(LOGOUT, headers=headers)
    assert response.status_code == 403
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "about:blank"
    assert calls == []


@pytest.mark.parametrize("origin", [None, "https://evil.example"])
def test_password_change_accepts_missing_or_foreign_origin(browser_app, origin) -> None:
    app, token, _, _, calls = browser_app
    headers = {"Authorization": f"Bearer {token}"}
    if origin is not None:
        headers["Origin"] = origin
    with TestClient(app) as client:
        response = client.post(
            CHANGE,
            headers=headers,
            json={"current_password": "old-password", "new_password": "new-password"},
        )
    assert response.status_code == 204
    assert response.headers["cache-control"] == "no-store"
    assert "Max-Age=0" in response.headers["set-cookie"]
    if origin is not None:
        assert "access-control-allow-origin" not in response.headers
    assert [call[0] for call in calls] == ["password"]


def test_password_change_requires_https_in_production(
    browser_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, token, _, _, calls = browser_app
    monkeypatch.setattr(app.state.settings, "environment", "production")
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"current_password": "old-password", "new_password": "new-password"}
    with TestClient(app, base_url="http://testserver") as client:
        insecure = client.post(CHANGE, headers=headers, json=payload)
    with TestClient(app, base_url="https://testserver") as client:
        secure = client.post(CHANGE, headers=headers, json=payload)
    assert insecure.status_code == 403
    assert insecure.headers["content-type"] == "application/problem+json"
    assert secure.status_code == 204
    assert [call[0] for call in calls] == ["password"]


def test_wrong_current_password_is_safe_problem(browser_app) -> None:
    app, token, _, current_session, calls = browser_app
    with TestClient(app) as client:
        response = client.post(
            CHANGE,
            headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN},
            json={"current_password": "wrong-secret", "new_password": "new-secret"},
        )
    assert response.status_code == 403
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert (
        response.json()["type"] == "urn:dreamplanner:problem:invalid-current-password"
    )
    assert "wrong-secret" not in response.text
    assert "new-secret" not in response.text
    assert current_session.revoked_at is None
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ('{"current_password":"old-password","new_password":""}', 422),
        ('{"current_password":"old-password","new_password":42}', 422),
        ("{invalid", 400),
    ],
)
def test_password_change_rejects_invalid_json_or_password(
    browser_app, body, status
) -> None:
    app, token, _, _, calls = browser_app
    with TestClient(app) as client:
        response = client.post(
            CHANGE,
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": ORIGIN,
                "Content-Type": "application/json",
            },
            content=body,
        )
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    assert "old-password" not in response.text
    assert calls == []


def test_password_change_is_rate_limited(browser_app) -> None:
    app, token, _, _, _ = browser_app
    headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}
    with TestClient(app) as client:
        for _ in range(10):
            response = client.post(
                CHANGE,
                headers=headers,
                json={"current_password": "wrong", "new_password": "new"},
            )
            assert response.status_code == 403
        limited = client.post(
            CHANGE,
            headers=headers,
            json={"current_password": "wrong", "new_password": "new"},
        )
    assert limited.status_code == 429
    assert limited.json()["type"] == "urn:dreamplanner:problem:rate-limited"
    assert int(limited.headers["retry-after"]) > 0


def test_openapi_marks_both_commands_as_bearer_protected(browser_app) -> None:
    app, _, _, _, _ = browser_app
    paths = app.openapi()["paths"]
    for path, method in ((LOGOUT, "delete"), (CHANGE, "post")):
        operation = paths[path][method]
        assert operation["security"] == [{"BearerAuth": []}]
        assert operation["responses"]["204"]["description"] == (
            "Operacja zakończona bez treści odpowiedzi."
        )
        for status in ("401", "403"):
            assert (
                "application/problem+json" in operation["responses"][status]["content"]
            )
    assert (
        "application/problem+json"
        in paths[CHANGE]["post"]["responses"]["429"]["content"]
    )
