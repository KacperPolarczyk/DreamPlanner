"""HTTP contract checks for administrator account activation commands."""

import base64
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from src.auth import session_service
from src.core.database import get_session
from src.main import create_app
from src.users import router as users_router
from src.users.account_service import (
    AccountNotFound,
    AccountStateConflict,
    EmailConflict,
)
from src.users.schemas import IssuedActivation

PATH = "/api/v1/users"
USER_ID = 9007199254740993
NOW = datetime(2027, 1, 6, 10, tzinfo=UTC)


@pytest.fixture
def admin_api(monkeypatch: pytest.MonkeyPatch):
    """Use real signed Bearer validation and controlled account commands."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "admin-api-test-v1")
    monkeypatch.setenv("JWT_ISSUER", "dreamplanner-admin-api-test")
    monkeypatch.setenv("JWT_AUDIENCE", "dreamplanner-admin-api")
    app = create_app()
    session_id = uuid4()
    actor = SimpleNamespace(
        id=42,
        is_active=True,
        activated_at=NOW,
        role=SimpleNamespace(code="admin"),
    )
    auth_session = SimpleNamespace(
        user_id=42,
        revoked_at=None,
        idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
        absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    account = SimpleNamespace(
        id=USER_ID,
        email="jan@example.com",
        full_name="Jan Kowalski",
        role=SimpleNamespace(code="employee"),
        is_active=False,
        activated_at=None,
        created_at=NOW,
        updated_at=NOW,
        password_hash="hidden-password-hash",
        token_hash="hidden-token-hash",
        refresh_token="hidden-refresh-token",
    )

    class Session:
        """Provide the created account for its public response projection."""

        def scalar(self, statement):
            assert list(statement.compile().params.values()) == [USER_ID]
            return account

    app.dependency_overrides[get_session] = Session

    class AuthRepository:
        """Supply current account and session state to shared JWT validation."""

        def get_session(self, requested_id):
            return auth_session if requested_id == session_id else None

        def get_user_fresh(self, requested_id):
            return actor if requested_id == 42 else None

    monkeypatch.setattr(
        session_service, "AuthSessionRepository", lambda session: AuthRepository()
    )
    calls = []

    def create(session, actor_id, data):
        calls.append(("create", actor_id, data.role))
        account.role.code = data.role
        account.email = data.email
        account.full_name = data.full_name
        return IssuedActivation(
            user_id=USER_ID,
            activation_token=SecretStr("first-secret"),
            expires_at=NOW + timedelta(days=1),
        )

    def reissue(session, actor_id, user_id):
        calls.append(("reissue", actor_id, user_id))
        return IssuedActivation(
            user_id=user_id,
            activation_token=SecretStr("replacement-secret"),
            expires_at=NOW + timedelta(days=1, hours=1),
        )

    monkeypatch.setattr(users_router, "create_user", create)
    monkeypatch.setattr(users_router, "reissue_activation_token", reissue)
    token = app.state.jwt_codec.issue(42, session_id)
    return app, token, actor, account, calls


@pytest.mark.parametrize("role", ["manager", "employee"])
def test_admin_creates_inactive_account_with_only_contract_fields(
    admin_api, role
) -> None:
    app, token, _, account, calls = admin_api
    with TestClient(app) as client:
        response = client.post(
            PATH,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "email": "jan@example.com",
                "full_name": "Jan Kowalski",
                "role": role,
            },
        )
    assert response.status_code == 201
    assert response.headers["location"] == f"/api/v1/users/{USER_ID}"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "id": str(USER_ID),
        "email": "jan@example.com",
        "full_name": "Jan Kowalski",
        "role": role,
        "is_active": False,
        "activated_at": None,
        "created_at": "2027-01-06T10:00:00Z",
        "updated_at": "2027-01-06T10:00:00Z",
        "activation_token": "first-secret",
        "activation_expires_at": "2027-01-07T10:00:00Z",
    }
    assert account.is_active is False
    assert "hidden-" not in response.text
    assert calls == [("create", 42, role)]


def test_admin_cannot_create_admin_role(admin_api) -> None:
    app, token, _, _, calls = admin_api
    with TestClient(app) as client:
        response = client.post(
            PATH,
            headers={"Authorization": f"Bearer {token}"},
            json={"email": "jan@example.com", "full_name": "Jan", "role": "admin"},
        )
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert calls == []


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ("{invalid", 400),
        ('{"email":"invalid","full_name":"Jan","role":"employee"}', 422),
    ],
)
def test_create_rejects_invalid_request(admin_api, body, status) -> None:
    app, token, _, _, calls = admin_api
    with TestClient(app) as client:
        response = client.post(
            PATH,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            content=body,
        )
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    assert calls == []


@pytest.mark.parametrize("role", ["manager", "employee"])
def test_non_admin_is_denied_before_account_operation(admin_api, role) -> None:
    app, token, actor, _, calls = admin_api
    actor.role.code = role
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        created = client.post(
            PATH,
            headers=headers,
            json={"email": "jan@example.com", "full_name": "Jan", "role": "employee"},
        )
        reissued = client.post(f"{PATH}/{USER_ID}/activation-tokens", headers=headers)
    for response in (created, reissued):
        assert response.status_code == 403
        assert response.headers["content-type"] == "application/problem+json"
        assert response.headers["cache-control"] == "no-store"
    assert calls == []


@pytest.mark.parametrize("authorization", [None, "Bearer invalid"])
def test_commands_require_valid_bearer(admin_api, authorization) -> None:
    app, _, _, _, calls = admin_api
    headers = {"Authorization": authorization} if authorization else {}
    with TestClient(app) as client:
        created = client.post(
            PATH,
            headers=headers,
            json={"email": "jan@example.com", "full_name": "Jan", "role": "employee"},
        )
        reissued = client.post(f"{PATH}/{USER_ID}/activation-tokens", headers=headers)
    for response in (created, reissued):
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.headers["content-type"] == "application/problem+json"
    assert calls == []


def test_reissue_returns_only_new_token(admin_api) -> None:
    app, token, _, _, calls = admin_api
    with TestClient(app) as client:
        response = client.post(
            f"{PATH}/{USER_ID}/activation-tokens",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "activation_token": "replacement-secret",
        "expires_at": "2027-01-07T11:00:00Z",
    }
    assert "hidden-" not in response.text
    assert "first-secret" not in response.text
    assert calls == [("reissue", 42, USER_ID)]


def test_create_conflict(admin_api, monkeypatch) -> None:
    app, token, _, _, _ = admin_api

    def fail(*args):
        raise EmailConflict()

    monkeypatch.setattr(users_router, "create_user", fail)
    with TestClient(app) as client:
        response = client.post(
            PATH,
            headers={"Authorization": f"Bearer {token}"},
            json={"email": "jan@example.com", "full_name": "Jan", "role": "employee"},
        )
    assert response.status_code == 409
    assert response.json()["type"] == "urn:dreamplanner:problem:email-conflict"
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize(
    ("error", "status", "kind"),
    [
        (AccountNotFound, 404, "about:blank"),
        (AccountStateConflict, 409, "urn:dreamplanner:problem:account-state-conflict"),
    ],
)
def test_reissue_state_errors(admin_api, monkeypatch, error, status, kind) -> None:
    app, token, _, _, _ = admin_api

    def fail(*args):
        raise error()

    monkeypatch.setattr(users_router, "reissue_activation_token", fail)
    with TestClient(app) as client:
        response = client.post(
            f"{PATH}/{USER_ID}/activation-tokens",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == status
    assert response.json()["type"] == kind
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize("user_id", ["bad", "-1", "9223372036854775808"])
def test_reissue_rejects_invalid_bigint(admin_api, user_id) -> None:
    app, token, _, _, calls = admin_api
    with TestClient(app) as client:
        response = client.post(
            f"{PATH}/{user_id}/activation-tokens",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 422
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert calls == []


def test_openapi_documents_admin_commands_and_bearer(admin_api) -> None:
    app, _, _, _, _ = admin_api
    schema = app.openapi()
    for path in (PATH, f"{PATH}/{{user_id}}/activation-tokens"):
        operation = schema["paths"][path]["post"]
        assert operation["security"] == [{"BearerAuth": []}]
        assert "application/problem+json" in operation["responses"]["401"]["content"]
    fields = schema["components"]["schemas"]["CreatedUserResponse"]["properties"]
    assert set(fields) == {
        "id",
        "email",
        "full_name",
        "role",
        "is_active",
        "activated_at",
        "created_at",
        "updated_at",
        "activation_token",
        "activation_expires_at",
    }
