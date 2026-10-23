"""HTTP and transaction boundary checks for account reactivation."""

import base64
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from src.auth import router as auth_router
from src.auth import session_service
from src.core.database import get_session
from src.main import create_app
from src.users import router as users_router
from src.users.account_service import (
    AccountStateConflict,
    InvalidActionToken,
    issue_reactivation_token,
    reactivate_account,
)
from src.users.schemas import IssuedReactivation, ReactivateAccountInput
from src.users.validation import AccountValidationError

ORIGIN = "http://localhost:5173"
NOW = datetime(2027, 1, 5, 12, tzinfo=UTC)


@pytest.fixture
def reactivation_app(monkeypatch: pytest.MonkeyPatch):
    """Keep real HTTP authentication with controlled persistence services."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "reactivation-test-v1")
    monkeypatch.setenv("JWT_ISSUER", "dreamplanner-reactivation-test")
    monkeypatch.setenv("JWT_AUDIENCE", "dreamplanner-reactivation-api")
    app = create_app()
    app.dependency_overrides[get_session] = lambda: object()
    sid = uuid4()
    admin = SimpleNamespace(
        id=42, is_active=True, activated_at=NOW, role=SimpleNamespace(code="admin")
    )
    target = SimpleNamespace(
        id=15,
        email="worker@example.com",
        full_name="Worker",
        role=SimpleNamespace(code="employee"),
        is_active=False,
        activated_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )

    class AuthRepository:
        def get_session(self, requested_id):
            if requested_id == sid:
                return SimpleNamespace(
                    user_id=42,
                    revoked_at=None,
                    idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
                    absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            return None

        def get_user_fresh(self, requested_id):
            return admin if requested_id == 42 else None

    class UserRepository:
        def __init__(self, session):
            pass

        def get_by_id(self, user_id):
            return target if user_id == target.id else None

    class TokenRepository:
        def __init__(self, session):
            pass

        def find_password_reset_by_hash(self, digest):
            return (
                SimpleNamespace(user_id=15)
                if digest == auth_router.hash_action_token("known")
                else None
            )

    monkeypatch.setattr(
        session_service, "AuthSessionRepository", lambda _: AuthRepository()
    )
    monkeypatch.setattr(users_router, "UserRepository", UserRepository)
    monkeypatch.setattr(auth_router, "ActionTokenRepository", TokenRepository)
    calls = []

    def issue(session, actor_id, user_id):
        calls.append(("issue", actor_id, user_id))
        return IssuedReactivation(
            user_id=user_id,
            reactivation_token=SecretStr("private-token"),
            expires_at=NOW + timedelta(days=1),
        )

    def consume(session, data):
        calls.append(("consume", data.token.get_secret_value()))

    monkeypatch.setattr(users_router, "issue_reactivation_token", issue)
    monkeypatch.setattr(auth_router, "reactivate_account", consume)
    return app, app.state.jwt_codec.issue(42, sid), admin, target, calls


def test_admin_issues_once_with_public_fields(reactivation_app) -> None:
    app, jwt, _, target, calls = reactivation_app
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/users/{target.id}/reactivation",
            headers={"Authorization": f"Bearer {jwt}"},
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["reactivation_token"] == "private-token"
    assert response.json()["role"] == "employee"
    assert response.json()["is_active"] is False
    assert response.json()["activated_at"] == "2027-01-05T12:00:00Z"
    assert calls == [("issue", 42, target.id)]


@pytest.mark.parametrize("role", ["manager", "employee"])
def test_non_admin_cannot_issue(reactivation_app, role) -> None:
    app, jwt, admin, target, calls = reactivation_app
    admin.role.code = role
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/users/{target.id}/reactivation",
            headers={"Authorization": f"Bearer {jwt}"},
        )
    assert response.status_code == 403
    assert response.headers["content-type"] == "application/problem+json"
    assert calls == []


@pytest.mark.parametrize("authorization", [None, "Bearer invalid"])
def test_issue_requires_bearer(reactivation_app, authorization) -> None:
    app, _, _, target, calls = reactivation_app
    headers = {"Authorization": authorization} if authorization else {}
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/users/{target.id}/reactivation", headers=headers
        )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert calls == []


def test_public_reactivation_has_no_bearer_or_session(reactivation_app) -> None:
    app, _, _, _, calls = reactivation_app
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/reactivations",
            headers={"Origin": ORIGIN},
            json={"token": "known", "new_password": "new-password"},
        )
    assert response.status_code == 204
    assert response.content == b""
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert calls == [("consume", "known")]


@pytest.mark.parametrize("origin", [None, ORIGIN, "https://evil.example"])
def test_public_reactivation_does_not_require_origin(reactivation_app, origin) -> None:
    app, _, _, _, calls = reactivation_app
    headers = {"Origin": origin} if origin is not None else {}
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/reactivations",
            headers=headers,
            json={"token": "known", "new_password": "new-password"},
        )
    assert response.status_code == 204
    assert "set-cookie" not in response.headers
    if origin == "https://evil.example":
        assert "access-control-allow-origin" not in response.headers
    assert calls == [("consume", "known")]


def test_public_reactivation_requires_https_in_production(
    reactivation_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _, _, _, calls = reactivation_app
    monkeypatch.setattr(app.state.settings, "environment", "production")
    payload = {"token": "known", "new_password": "new-password"}
    with TestClient(app, base_url="http://testserver") as client:
        insecure = client.post("/api/v1/auth/reactivations", json=payload)
    with TestClient(app, base_url="https://testserver") as client:
        secure = client.post("/api/v1/auth/reactivations", json=payload)
    assert insecure.status_code == 403
    assert insecure.headers["content-type"] == "application/problem+json"
    assert secure.status_code == 204
    assert calls == [("consume", "known")]


def test_public_invalid_token_is_uniform_problem(reactivation_app, monkeypatch) -> None:
    app, _, _, _, _ = reactivation_app

    def reject(session, data):
        raise InvalidActionToken("Invalid action token.")

    monkeypatch.setattr(auth_router, "reactivate_account", reject)
    with TestClient(app) as client:
        known = client.post(
            "/api/v1/auth/reactivations",
            headers={"Origin": ORIGIN},
            json={"token": "known", "new_password": "new-password"},
        )
        unknown = client.post(
            "/api/v1/auth/reactivations",
            headers={"Origin": ORIGIN},
            json={"token": "unknown", "new_password": "new-password"},
        )
    assert known.status_code == unknown.status_code == 401
    assert known.json() == unknown.json()
    assert known.json()["type"] == "urn:dreamplanner:problem:invalid-action-token"
    assert known.headers["cache-control"] == "no-store"
    assert "www-authenticate" not in known.headers


def test_public_password_validation_problem(reactivation_app, monkeypatch) -> None:
    app, _, _, _, _ = reactivation_app

    def reject(session, data):
        raise AccountValidationError("Password must not be empty.")

    monkeypatch.setattr(auth_router, "reactivate_account", reject)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/reactivations",
            headers={"Origin": ORIGIN},
            json={"token": "known", "new_password": ""},
        )
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"


def test_reactivation_ip_limit_counts_invalid_json(reactivation_app) -> None:
    app, _, _, _, calls = reactivation_app
    with TestClient(app, client=("198.51.100.40", 40000)) as client:
        for _ in range(20):
            response = client.post(
                "/api/v1/auth/reactivations",
                headers={"Origin": ORIGIN, "Content-Type": "application/json"},
                content="{invalid",
            )
            assert response.status_code == 400
        blocked = client.post(
            "/api/v1/auth/reactivations",
            headers={"Origin": ORIGIN},
            json={"token": "known", "new_password": "new-password"},
        )
    assert blocked.status_code == 429
    assert blocked.headers["content-type"] == "application/problem+json"
    assert int(blocked.headers["retry-after"]) >= 1
    assert calls == []


def test_issue_active_account_maps_state_conflict(
    reactivation_app, monkeypatch
) -> None:
    app, jwt, _, target, _ = reactivation_app

    def reject(session, actor_id, user_id):
        raise AccountStateConflict("Account is not deactivated.")

    monkeypatch.setattr(users_router, "issue_reactivation_token", reject)
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/users/{target.id}/reactivation",
            headers={"Authorization": f"Bearer {jwt}"},
        )
    assert response.status_code == 409
    assert response.json()["type"] == "urn:dreamplanner:problem:account-state-conflict"


def test_openapi_documents_reactivation(reactivation_app) -> None:
    app, _, _, _, _ = reactivation_app
    paths = app.openapi()["paths"]
    issued = paths["/api/v1/users/{user_id}/reactivation"]["post"]
    consumed = paths["/api/v1/auth/reactivations"]["post"]
    assert issued["security"] == [{"BearerAuth": []}]
    assert issued["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReactivationResponse"
    }
    assert "security" not in consumed
    assert "204" in consumed["responses"]
    assert consumed["responses"]["403"]["description"] == (
        "HTTPS required in production"
    )
    for status in ("400", "401", "403", "422", "429"):
        assert "application/problem+json" in consumed["responses"][status]["content"]


def test_issue_rolls_back_on_commit_failure() -> None:
    session = MagicMock()
    user = SimpleNamespace(
        is_active=False, activated_at=NOW, updated_at=NOW, password_hash="old"
    )
    session.scalar.side_effect = [42, user]
    session.commit.side_effect = RuntimeError("commit failed")
    with pytest.raises(RuntimeError, match="commit failed"):
        issue_reactivation_token(session, 42, 15)
    session.rollback.assert_called_once_with()


def test_consume_rolls_back_on_commit_failure() -> None:
    session = MagicMock()
    user = SimpleNamespace(
        is_active=False, activated_at=NOW, updated_at=NOW, password_hash="old"
    )
    token = SimpleNamespace(
        id=uuid4(),
        user_id=15,
        purpose="password_reset",
        token_hash=auth_router.hash_action_token("known"),
        used_at=None,
        revoked_at=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session.scalar.side_effect = [token, user, token]
    session.commit.side_effect = RuntimeError("commit failed")
    with pytest.raises(RuntimeError, match="commit failed"):
        reactivate_account(
            session,
            ReactivateAccountInput(
                token=SecretStr("known"), new_password=SecretStr("new-password")
            ),
        )
    session.rollback.assert_called_once_with()


def test_empty_password_is_rejected_before_token_lookup() -> None:
    session = MagicMock()
    with pytest.raises(AccountValidationError):
        reactivate_account(
            session,
            ReactivateAccountInput(
                token=SecretStr("known"), new_password=SecretStr("")
            ),
        )
    session.scalar.assert_not_called()
    session.rollback.assert_called_once_with()


def test_active_account_cannot_receive_token() -> None:
    session = MagicMock()
    session.scalar.side_effect = [42, SimpleNamespace(is_active=True, activated_at=NOW)]
    with pytest.raises(AccountStateConflict):
        issue_reactivation_token(session, 42, 15)
    session.commit.assert_not_called()


def test_invalid_token_is_rejected_without_secret_in_error() -> None:
    session = MagicMock()
    session.scalar.return_value = None
    with pytest.raises(InvalidActionToken) as failure:
        reactivate_account(
            session,
            ReactivateAccountInput(
                token=SecretStr("private-token"), new_password=SecretStr("new-password")
            ),
        )
    assert "private-token" not in str(failure.value)
