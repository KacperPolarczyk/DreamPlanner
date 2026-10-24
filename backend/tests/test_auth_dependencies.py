"""HTTP and unit tests for shared authentication and role dependencies."""

import base64
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Annotated
from uuid import uuid4

import jwt
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from src.auth import router as auth_router
from src.auth import session_service
from src.auth.dependencies import CurrentIdentity, require_roles
from src.auth.session_service import ActiveIdentity
from src.core.database import get_session
from src.main import create_app


@pytest.fixture
def protected_app(monkeypatch: pytest.MonkeyPatch):
    """Use real JWT decoding and session validation with controlled DB rows."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "rbac-test-v1")
    monkeypatch.setenv("JWT_ISSUER", "dreamplanner-rbac-test")
    monkeypatch.setenv("JWT_AUDIENCE", "dreamplanner-rbac-api")
    app = create_app()
    app.dependency_overrides[get_session] = lambda: object()
    user_id = 42
    session_id = uuid4()
    now = datetime.now(UTC)
    auth_session = SimpleNamespace(
        user_id=user_id,
        revoked_at=None,
        idle_expires_at=now + timedelta(hours=1),
        absolute_expires_at=now + timedelta(days=1),
    )
    user = SimpleNamespace(
        is_active=True,
        activated_at=now,
        role=SimpleNamespace(code="employee"),
    )

    class Repository:
        """Expose current state each time validation reads a record."""

        def get_session(self, requested_id):
            return auth_session if requested_id == session_id else None

        def get_user_fresh(self, requested_id):
            return user if requested_id == user_id else None

    monkeypatch.setattr(
        session_service, "AuthSessionRepository", lambda session: Repository()
    )

    @app.get("/test/identity")
    def identity(identity: CurrentIdentity) -> dict[str, str]:
        return {"user_id": str(identity.user_id), "role": identity.role}

    @app.get("/test/admin")
    def admin(
        identity: Annotated[ActiveIdentity, Depends(require_roles("admin"))],
    ) -> dict[str, str]:
        return {"role": identity.role}

    @app.get("/test/staff")
    def staff(
        identity: Annotated[
            ActiveIdentity, Depends(require_roles("manager", "employee"))
        ],
    ) -> dict[str, str]:
        return {"role": identity.role}

    token = app.state.jwt_codec.issue(user_id, session_id)
    return app, token, auth_session, user


def test_require_roles_rejects_empty_or_unknown_policies() -> None:
    """A route must declare at least one supported role explicitly."""
    with pytest.raises(ValueError, match="At least one role"):
        require_roles()
    with pytest.raises(ValueError, match="Unsupported role"):
        require_roles("owner")


def test_identity_and_explicit_role_access(protected_app) -> None:
    """Return current identity and accept either listed role without hierarchy."""
    app, token, _, user = protected_app
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        identity = client.get("/test/identity", headers=headers)
        assert identity.status_code == 200
        assert identity.json() == {"user_id": "42", "role": "employee"}
        assert client.get("/test/staff", headers=headers).json() == {"role": "employee"}
        user.role.code = "manager"
        assert client.get("/test/staff", headers=headers).json() == {"role": "manager"}
        user.role.code = "admin"
        assert client.get("/test/admin", headers=headers).json() == {"role": "admin"}
        denied = client.get("/test/staff", headers=headers)
        assert denied.status_code == 403


def test_wrong_role_is_problem_details_without_bearer_challenge(protected_app) -> None:
    """A valid session with a disallowed role receives 403, not 401."""
    app, token, _, _ = protected_app
    with TestClient(app) as client:
        response = client.get(
            "/test/admin", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 403
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert "www-authenticate" not in response.headers
    assert response.json() == {
        "type": "about:blank",
        "title": "Odmowa dostępu",
        "status": 403,
        "detail": "Brak uprawnienia.",
    }


@pytest.mark.parametrize("authorization", [None, "Basic bad", "Bearer bad"])
def test_missing_or_invalid_bearer_is_401(protected_app, authorization) -> None:
    """Missing and malformed credentials preserve the Bearer challenge."""
    app, _, _, _ = protected_app
    headers = {"Authorization": authorization} if authorization else {}
    with TestClient(app) as client:
        response = client.get("/test/identity", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": "about:blank",
        "title": "Brak uwierzytelnienia",
        "status": 401,
        "detail": "Nieprawidłowe uwierzytelnienie.",
    }


def test_expired_jwt_is_401(protected_app) -> None:
    """An expired but correctly signed access token cannot reach the database."""
    app, token, _, _ = protected_app
    codec = app.state.jwt_codec
    claims = jwt.decode(
        token,
        codec.keys[codec.current_kid],
        algorithms=["HS256"],
        audience=codec.audience,
        issuer=codec.issuer,
    )
    claims["exp"] = int(datetime.now(UTC).timestamp()) - 1
    expired = jwt.encode(
        claims,
        codec.keys[codec.current_kid],
        algorithm="HS256",
        headers={"kid": codec.current_kid},
    )
    with TestClient(app) as client:
        response = client.get(
            "/test/identity", headers={"Authorization": f"Bearer {expired}"}
        )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("state", ["revoked", "inactive", "unactivated"])
def test_invalid_session_or_account_is_401(protected_app, state) -> None:
    """Database state blocks access even while the JWT remains valid."""
    app, token, auth_session, user = protected_app
    if state == "revoked":
        auth_session.revoked_at = datetime.now(UTC)
    elif state == "inactive":
        user.is_active = False
    else:
        user.activated_at = None
    with TestClient(app) as client:
        response = client.get(
            "/test/admin", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_protected_dependency_declares_bearer_in_openapi(protected_app) -> None:
    """Nested role dependencies keep Swagger's Authorize security scheme."""
    app, _, _, _ = protected_app
    schema = app.openapi()
    assert schema["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": (
            "Token dostępu JWT w nagłówku Authorization ze schematem Bearer."
        ),
    }
    for path in ("/test/identity", "/test/admin", "/test/staff"):
        assert schema["paths"][path]["get"]["security"] == [{"BearerAuth": []}]


def test_logout_keeps_origin_check_and_session_identity(
    protected_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Logout still revokes the caller's session and clears its cookie."""
    app, token, _, _ = protected_app
    calls = []
    monkeypatch.setattr(
        auth_router,
        "revoke_session",
        lambda session, session_id, user_id: calls.append((session_id, user_id)),
    )
    path = "/api/v1/auth/sessions/current"
    with TestClient(app) as client:
        denied_origin = client.delete(path, headers={"Authorization": "Bearer bad"})
        assert denied_origin.status_code == 403
        assert calls == []
        logout = client.delete(
            path,
            headers={
                "Origin": "http://localhost:5173",
                "Authorization": f"Bearer {token}",
            },
        )
    assert logout.status_code == 204
    assert logout.text == ""
    assert calls == [(app.state.jwt_codec.verify(token)[1], 42)]
    assert "Path=/api/v1/auth/sessions/current" in logout.headers["set-cookie"]
    assert "Max-Age=0" in logout.headers["set-cookie"]
