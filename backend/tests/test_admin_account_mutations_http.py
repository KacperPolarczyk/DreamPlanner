"""HTTP contracts for protected role changes and account deactivation."""

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
from src.users.account_service import (
    AccountNotFound,
    AccountStateConflict,
    AdministratorRequired,
)
from src.users.admin_service import LastAdminConflict, SelfActionForbidden

PATH = "/api/v1/users"
NOW = datetime(2027, 1, 5, 12, tzinfo=UTC)


@pytest.fixture
def admin_mutations(monkeypatch: pytest.MonkeyPatch):
    """Exercise real JWT and RBAC dependencies with controlled write services."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "admin-mutations-test-v1")
    monkeypatch.setenv("JWT_ISSUER", "dreamplanner-admin-mutations-test")
    monkeypatch.setenv("JWT_AUDIENCE", "dreamplanner-admin-mutations-api")
    app = create_app()
    admin_sid, target_sid = uuid4(), uuid4()
    admin = SimpleNamespace(
        id=42, is_active=True, activated_at=NOW, role=SimpleNamespace(code="admin")
    )
    target = SimpleNamespace(
        id=9007199254740993,
        email="worker@example.com",
        full_name="Worker",
        role=SimpleNamespace(code="employee"),
        is_active=True,
        activated_at=NOW,
        created_at=NOW,
        updated_at=NOW,
        password_hash="hidden-password-hash",
        refresh_token="hidden-refresh-token",
        token_hash="hidden-token-hash",
    )
    sessions = {
        admin_sid: SimpleNamespace(
            user_id=42,
            revoked_at=None,
            idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
            absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
        target_sid: SimpleNamespace(
            user_id=target.id,
            revoked_at=None,
            idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
            absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
    }
    app.dependency_overrides[get_session] = lambda: object()

    class AuthRepository:
        """Expose live session and role changes to shared Bearer validation."""

        def get_session(self, requested_id):
            return sessions.get(requested_id)

        def get_user_fresh(self, requested_id):
            return {42: admin, target.id: target}.get(requested_id)

    monkeypatch.setattr(
        session_service, "AuthSessionRepository", lambda session: AuthRepository()
    )

    class UserRepository:
        """Serve the target's public profile after a mutation."""

        def __init__(self, session):
            pass

        def get_by_id(self, user_id):
            return target if user_id == target.id else None

    monkeypatch.setattr(users_router, "UserRepository", UserRepository)
    calls = []

    def change(session, actor_id, target_id, role):
        calls.append(("role", actor_id, target_id, role))
        target.role.code = role
        target.updated_at = NOW + timedelta(hours=1)
        return target

    def deactivate(session, actor_id, target_id):
        calls.append(("deactivation", actor_id, target_id))
        target.is_active = False
        target.updated_at = NOW + timedelta(hours=1)
        sessions[target_sid].revoked_at = datetime.now(UTC)
        return target

    monkeypatch.setattr(users_router, "change_role", change)
    monkeypatch.setattr(users_router, "deactivate_account", deactivate)
    admin_token = app.state.jwt_codec.issue(42, admin_sid)
    target_token = app.state.jwt_codec.issue(target.id, target_sid)
    return app, admin_token, target_token, admin, target, calls


def test_admin_changes_role_and_new_permissions_apply_immediately(
    admin_mutations,
) -> None:
    app, admin_token, target_token, _, target, calls = admin_mutations
    with TestClient(app) as client:
        changed = client.put(
            f"{PATH}/{target.id}/role",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"role": "manager"},
        )
        own_profile = client.get(
            f"{PATH}/me", headers={"Authorization": f"Bearer {target_token}"}
        )
    assert changed.status_code == 200
    assert changed.headers["cache-control"] == "no-store"
    assert changed.json() == {
        "id": str(target.id),
        "email": "worker@example.com",
        "full_name": "Worker",
        "role": "manager",
        "is_active": True,
        "activated_at": "2027-01-05T12:00:00Z",
        "created_at": "2027-01-05T12:00:00Z",
        "updated_at": "2027-01-05T13:00:00Z",
    }
    assert "hidden-" not in changed.text
    assert own_profile.status_code == 200
    assert own_profile.json()["role"] == "manager"
    assert calls == [("role", 42, target.id, "manager")]


def test_admin_deactivates_account_and_old_access_stops_working(
    admin_mutations,
) -> None:
    app, admin_token, target_token, _, target, calls = admin_mutations
    with TestClient(app) as client:
        response = client.post(
            f"{PATH}/{target.id}/deactivation",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        reuse = client.get(
            f"{PATH}/me", headers={"Authorization": f"Bearer {target_token}"}
        )
    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert response.json()["activated_at"] == "2027-01-05T12:00:00Z"
    assert response.headers["cache-control"] == "no-store"
    assert "hidden-" not in response.text
    assert reuse.status_code == 401
    assert reuse.headers["www-authenticate"] == "Bearer"
    assert calls == [("deactivation", 42, target.id)]


@pytest.mark.parametrize("role", ["manager", "employee"])
def test_non_admin_is_denied_before_service_call(admin_mutations, role) -> None:
    app, token, _, admin, target, calls = admin_mutations
    admin.role.code = role
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        responses = [
            client.put(
                f"{PATH}/{target.id}/role", headers=headers, json={"role": "admin"}
            ),
            client.post(f"{PATH}/{target.id}/deactivation", headers=headers),
        ]
    assert [response.status_code for response in responses] == [403, 403]
    assert all(
        r.headers["content-type"] == "application/problem+json" for r in responses
    )
    assert calls == []


@pytest.mark.parametrize("authorization", [None, "Bearer invalid"])
def test_mutations_require_valid_bearer(admin_mutations, authorization) -> None:
    app, _, _, _, target, calls = admin_mutations
    headers = {"Authorization": authorization} if authorization else {}
    with TestClient(app) as client:
        responses = [
            client.put(
                f"{PATH}/{target.id}/role", headers=headers, json={"role": "admin"}
            ),
            client.post(f"{PATH}/{target.id}/deactivation", headers=headers),
        ]
    assert [response.status_code for response in responses] == [401, 401]
    assert all(r.headers["www-authenticate"] == "Bearer" for r in responses)
    assert calls == []


@pytest.mark.parametrize(
    ("error", "status", "kind"),
    [
        (SelfActionForbidden, 403, "about:blank"),
        (AdministratorRequired, 403, "about:blank"),
        (AccountNotFound, 404, "about:blank"),
        (LastAdminConflict, 409, "urn:dreamplanner:problem:last-admin-conflict"),
    ],
)
def test_role_errors_are_mapped_to_contract_problems(
    admin_mutations, monkeypatch, error, status, kind
) -> None:
    app, token, _, _, target, _ = admin_mutations

    def fail(*args):
        raise error()

    monkeypatch.setattr(users_router, "change_role", fail)
    with TestClient(app) as client:
        response = client.put(
            f"{PATH}/{target.id}/role",
            headers={"Authorization": f"Bearer {token}"},
            json={"role": "manager"},
        )
    assert response.status_code == status
    assert response.json()["type"] == kind
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize(
    ("error", "status", "kind"),
    [
        (SelfActionForbidden, 403, "about:blank"),
        (AccountNotFound, 404, "about:blank"),
        (AccountStateConflict, 409, "urn:dreamplanner:problem:account-state-conflict"),
        (LastAdminConflict, 409, "urn:dreamplanner:problem:last-admin-conflict"),
    ],
)
def test_deactivation_errors_are_mapped_to_contract_problems(
    admin_mutations, monkeypatch, error, status, kind
) -> None:
    app, token, _, _, target, _ = admin_mutations

    def fail(*args):
        raise error()

    monkeypatch.setattr(users_router, "deactivate_account", fail)
    with TestClient(app) as client:
        response = client.post(
            f"{PATH}/{target.id}/deactivation",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == status
    assert response.json()["type"] == kind
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize("invalid_id", ["bad", "-1", "9223372036854775808"])
def test_invalid_path_id_is_422(admin_mutations, invalid_id) -> None:
    app, token, _, _, _, calls = admin_mutations
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        responses = [
            client.put(
                f"{PATH}/{invalid_id}/role", headers=headers, json={"role": "admin"}
            ),
            client.post(f"{PATH}/{invalid_id}/deactivation", headers=headers),
        ]
    assert [response.status_code for response in responses] == [422, 422]
    assert calls == []


@pytest.mark.parametrize("body", [{"role": "owner"}, {}, {"role": 42}])
def test_invalid_role_is_422_without_service_call(admin_mutations, body) -> None:
    app, token, _, _, target, calls = admin_mutations
    with TestClient(app) as client:
        response = client.put(
            f"{PATH}/{target.id}/role",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
    assert response.status_code == 422
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert calls == []


def test_malformed_role_json_is_400(admin_mutations) -> None:
    app, token, _, _, target, calls = admin_mutations
    with TestClient(app) as client:
        response = client.put(
            f"{PATH}/{target.id}/role",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            content="{invalid",
        )
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    assert calls == []


def test_openapi_declares_bearer_and_public_responses(admin_mutations) -> None:
    app, _, _, _, _, _ = admin_mutations
    paths = app.openapi()["paths"]
    for path, method in (
        (f"{PATH}/{{user_id}}/role", "put"),
        (f"{PATH}/{{user_id}}/deactivation", "post"),
    ):
        operation = paths[path][method]
        assert operation["security"] == [{"BearerAuth": []}]
        assert operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ] == {"$ref": "#/components/schemas/UserProfile"}
        for status in ("401", "403", "404", "409", "422"):
            assert (
                "application/problem+json" in operation["responses"][status]["content"]
            )
