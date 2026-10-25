"""HTTP contract tests for the authenticated account profile."""

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

PATH = "/api/v1/users/me"
USER_ID = 9007199254740993


@pytest.fixture
def profile_app(monkeypatch: pytest.MonkeyPatch):
    """Authenticate with a signed JWT and controlled database rows."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "profile-test-v1")
    monkeypatch.setenv("JWT_ISSUER", "dreamplanner-profile-test")
    monkeypatch.setenv("JWT_AUDIENCE", "dreamplanner-profile-api")
    app = create_app()
    session_id = uuid4()
    activated_at = datetime(2027, 1, 5, 12, tzinfo=UTC)
    user = SimpleNamespace(
        id=USER_ID,
        email="anna@example.com",
        full_name="Anna Nowak",
        role=SimpleNamespace(code="employee"),
        is_active=True,
        activated_at=activated_at,
        created_at=datetime(2027, 1, 4, 9, tzinfo=UTC),
        updated_at=activated_at,
        password_hash="never-return-this-hash",
        refresh_token="never-return-this-refresh-token",
        activation_token="never-return-this-activation-token",
    )
    auth_session = SimpleNamespace(
        user_id=USER_ID,
        revoked_at=None,
        idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
        absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
    )

    class Session:
        """Return only the account selected by the repository query."""

        def scalar(self, statement):
            params = statement.compile().params
            assert list(params.values()) == [USER_ID]
            return user

    app.dependency_overrides[get_session] = lambda: Session()

    class AuthRepository:
        """Expose mutable account and session state to real validation logic."""

        def get_session(self, requested_id):
            return auth_session if requested_id == session_id else None

        def get_user_fresh(self, requested_id):
            return user if requested_id == USER_ID else None

    monkeypatch.setattr(
        session_service, "AuthSessionRepository", lambda session: AuthRepository()
    )
    token = app.state.jwt_codec.issue(USER_ID, session_id)
    return app, token, user, auth_session


@pytest.mark.parametrize("role", ["admin", "manager", "employee"])
def test_profile_returns_only_current_public_fields(profile_app, role) -> None:
    """All roles receive the same current account shape with a string ID."""
    app, token, user, _ = profile_app
    user.role.code = role
    with TestClient(app) as client:
        response = client.get(PATH, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "id": str(USER_ID),
        "email": "anna@example.com",
        "full_name": "Anna Nowak",
        "role": role,
        "is_active": True,
        "activated_at": "2027-01-05T12:00:00Z",
        "created_at": "2027-01-04T09:00:00Z",
        "updated_at": "2027-01-05T12:00:00Z",
    }
    assert "never-return-this" not in response.text


@pytest.mark.parametrize("authorization", [None, "Bearer invalid"])
def test_profile_requires_valid_bearer(profile_app, authorization) -> None:
    """Missing or invalid access credentials receive the shared 401 problem."""
    app, _, _, _ = profile_app
    headers = {"Authorization": authorization} if authorization else {}
    with TestClient(app) as client:
        response = client.get(PATH, headers=headers)
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": "about:blank",
        "title": "Brak uwierzytelnienia",
        "status": 401,
        "detail": "Nieprawidłowe uwierzytelnienie.",
    }


@pytest.mark.parametrize("state", ["inactive", "revoked"])
def test_profile_rejects_invalid_account_or_session(profile_app, state) -> None:
    """A still signed JWT cannot read data after account or session invalidation."""
    app, token, user, auth_session = profile_app
    if state == "inactive":
        user.is_active = False
    else:
        auth_session.revoked_at = datetime.now(UTC)
    with TestClient(app) as client:
        response = client.get(PATH, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_profile_openapi_declares_bearer_and_exact_public_fields(profile_app) -> None:
    """Swagger marks the profile as protected and documents its public shape."""
    app, _, _, _ = profile_app
    operation = app.openapi()["paths"][PATH]["get"]
    assert operation["security"] == [{"BearerAuth": []}]
    assert "application/problem+json" in operation["responses"]["401"]["content"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/UserProfile"
    }
    fields = app.openapi()["components"]["schemas"]["UserProfile"]["properties"]
    assert set(fields) == {
        "id",
        "email",
        "full_name",
        "role",
        "is_active",
        "activated_at",
        "created_at",
        "updated_at",
    }
