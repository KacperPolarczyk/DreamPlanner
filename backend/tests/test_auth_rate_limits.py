"""Independent IP and identity limits on every public auth endpoint."""

import base64
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter
from pydantic import ValidationError
from sqlalchemy.orm import Session

from src.auth import router as auth_router
from src.auth.session_service import InvalidCredentials, InvalidSession, SessionGrant
from src.core.database import get_session
from src.main import create_app
from src.users.account_service import InvalidActionToken

ORIGIN = "http://localhost:5173"
LOGIN = "/api/v1/auth/sessions"
REFRESH = "/api/v1/auth/sessions/current/refresh"
ACTIVATE = "/api/v1/auth/activations"


@pytest.fixture
def limited_app(monkeypatch: pytest.MonkeyPatch):
    """Create isolated in-memory counters and predictable failed workflows."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "test-v1")
    monkeypatch.setenv("JWT_ISSUER", "dreamplanner-test")
    monkeypatch.setenv("JWT_AUDIENCE", "dreamplanner-api-test")
    app = create_app()
    app.dependency_overrides[get_session] = lambda: SimpleNamespace()

    def deny_login(session: Session, email: str, password: str, codec: object) -> None:
        raise InvalidCredentials("Invalid credentials.")

    def deny_refresh(session: Session, token: str, codec: object) -> None:
        raise InvalidSession("Invalid session.")

    def deny_activation(session: Session, data: object) -> None:
        raise InvalidActionToken("Invalid action token.")

    monkeypatch.setattr(auth_router, "login", deny_login)
    monkeypatch.setattr(auth_router, "refresh_session", deny_refresh)
    monkeypatch.setattr(auth_router, "activate_account", deny_activation)
    known_session_id = uuid4()
    monkeypatch.setattr(
        auth_router.AuthSessionRepository,
        "find_token_session_id",
        lambda self, digest: (
            known_session_id
            if digest == auth_router.hash_refresh_token("known")
            else None
        ),
    )
    monkeypatch.setattr(
        auth_router.ActionTokenRepository,
        "find_activation_by_hash",
        lambda self, digest: (
            SimpleNamespace(user_id=42)
            if digest == auth_router.hash_action_token("known")
            else None
        ),
    )
    return app


def _post(app, path: str, ip: str, **kwargs):
    """Send one request from an explicit peer IP and allowed browser origin."""
    with TestClient(app, client=(ip, 40000)) as client:
        return client.post(
            path, headers={"Origin": ORIGIN, **kwargs.pop("headers", {})}, **kwargs
        )


def _assert_limited(response) -> None:
    """Require the public problem shape and retry metadata."""
    assert response.status_code == 429
    assert response.headers["content-type"] == "application/problem+json"
    assert int(response.headers["retry-after"]) >= 1
    assert response.headers["x-ratelimit-remaining"] == "0"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": "urn:dreamplanner:problem:rate-limited",
        "title": "Zbyt wiele prób",
        "status": 429,
        "detail": "Spróbuj ponownie później.",
    }


def test_login_email_limit_is_independent_of_ip(limited_app) -> None:
    """Ten failed attempts across distinct IPs exhaust one email counter."""
    for index in range(10):
        response = _post(
            limited_app,
            LOGIN,
            f"198.51.100.{index + 1}",
            json={"email": "USER@example.com", "password": "wrong"},
        )
        assert response.status_code == 401
    blocked = _post(
        limited_app,
        LOGIN,
        "198.51.100.11",
        json={"email": "user@example.com", "password": "wrong"},
    )
    _assert_limited(blocked)
    other_email = _post(
        limited_app,
        LOGIN,
        "198.51.100.11",
        json={"email": "other@example.com", "password": "wrong"},
    )
    assert other_email.status_code == 401


def test_login_ip_limit_ignores_spoofed_forwarded_header(limited_app) -> None:
    """Twenty different emails still share the socket peer's IP counter."""
    for index in range(20):
        response = _post(
            limited_app,
            LOGIN,
            "198.51.100.20",
            headers={"X-Forwarded-For": f"203.0.113.{index + 1}"},
            json={"email": f"person{index}@example.com", "password": "wrong"},
        )
        assert response.status_code == 401
    blocked = _post(
        limited_app,
        LOGIN,
        "198.51.100.20",
        headers={"X-Forwarded-For": "203.0.113.250"},
        json={"email": "person21@example.com", "password": "wrong"},
    )
    _assert_limited(blocked)


def test_refresh_session_limit_is_independent_of_ip(limited_app) -> None:
    """Ten invalid attempts at one known session exhaust its counter."""
    for index in range(10):
        response = _post(
            limited_app,
            REFRESH,
            f"198.51.100.{index + 1}",
            headers={"Cookie": "dreamplanner_refresh=known"},
        )
        assert response.status_code == 401
    blocked = _post(
        limited_app,
        REFRESH,
        "198.51.100.11",
        headers={"Cookie": "dreamplanner_refresh=known"},
    )
    _assert_limited(blocked)
    unknown = _post(
        limited_app,
        REFRESH,
        "198.51.100.11",
        headers={"Cookie": "dreamplanner_refresh=other"},
    )
    assert unknown.status_code == 401


def test_refresh_ip_limit_counts_unknown_tokens(limited_app) -> None:
    """Sixty distinct unknown tokens cannot evade one peer IP counter."""
    for index in range(60):
        response = _post(
            limited_app,
            REFRESH,
            "198.51.100.30",
            headers={"Cookie": f"dreamplanner_refresh=unknown{index}"},
        )
        assert response.status_code == 401
    blocked = _post(
        limited_app,
        REFRESH,
        "198.51.100.30",
        headers={"Cookie": "dreamplanner_refresh=unknown61"},
    )
    _assert_limited(blocked)


def test_activation_account_limit_is_independent_of_ip(limited_app) -> None:
    """Five failed uses of a known account's token exhaust its counter."""
    for index in range(5):
        response = _post(
            limited_app,
            ACTIVATE,
            f"198.51.100.{index + 1}",
            json={"token": "known", "new_password": "wrong"},
        )
        assert response.status_code == 401
    blocked = _post(
        limited_app,
        ACTIVATE,
        "198.51.100.6",
        json={"token": "known", "new_password": "wrong"},
    )
    _assert_limited(blocked)
    unknown = _post(
        limited_app,
        ACTIVATE,
        "198.51.100.6",
        json={"token": "unknown", "new_password": "wrong"},
    )
    assert unknown.status_code == 401


def test_activation_ip_limit_counts_unknown_tokens(limited_app) -> None:
    """Twenty different invalid tokens still share the IP counter."""
    for index in range(20):
        response = _post(
            limited_app,
            ACTIVATE,
            "198.51.100.40",
            json={"token": f"unknown{index}", "new_password": "wrong"},
        )
        assert response.status_code == 401
    blocked = _post(
        limited_app,
        ACTIVATE,
        "198.51.100.40",
        json={"token": "unknown21", "new_password": "wrong"},
    )
    _assert_limited(blocked)


def test_malformed_login_still_consumes_ip_limit(limited_app) -> None:
    """Even requests rejected before endpoint validation use the IP counter."""
    for _ in range(20):
        response = _post(
            limited_app,
            LOGIN,
            "198.51.100.50",
            content=b"{bad-json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert response.headers["content-type"] == "application/problem+json"
        assert "bad-json" not in response.text
    blocked = _post(
        limited_app,
        LOGIN,
        "198.51.100.50",
        json={"email": "person@example.com", "password": "wrong"},
    )
    _assert_limited(blocked)


def test_invalid_storage_configuration_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse an unexpected backend and any configured memory fallback."""
    monkeypatch.setenv("RATE_LIMIT_STORAGE_URI", "redis://localhost:6379")
    with pytest.raises(ValidationError, match="memory://"):
        create_app()
    monkeypatch.setenv("RATE_LIMIT_STORAGE_URI", "memory://")
    monkeypatch.setenv("RATELIMIT_IN_MEMORY_FALLBACK_ENABLED", "true")
    with pytest.raises(ValueError, match="without fallback"):
        create_app()


def test_counter_keys_and_logs_never_contain_credentials(
    limited_app, caplog: pytest.LogCaptureFixture
) -> None:
    """Keep plaintext email, password and opaque tokens out of limiter state."""
    password = "not-a-real-password"
    email = "secret-address@example.com"
    refresh = "private-refresh-value"
    activation = "private-activation-value"
    _post(
        limited_app,
        LOGIN,
        "198.51.100.60",
        json={"email": email, "password": password},
    )
    _post(
        limited_app,
        REFRESH,
        "198.51.100.60",
        headers={"Cookie": f"dreamplanner_refresh={refresh}"},
    )
    _post(
        limited_app,
        ACTIVATE,
        "198.51.100.60",
        json={"token": activation, "new_password": password},
    )
    storage = limited_app.state.auth_rate_limits.limiter.limiter.storage
    recorded = repr(storage.storage) + repr(storage.events) + caplog.text
    for secret in (email, password, refresh, activation):
        assert secret not in recorded


def test_local_cookie_origin_and_openapi_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep refresh out of JSON and document the public problem media type."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "test-v1")
    monkeypatch.setenv("JWT_ISSUER", "dreamplanner-test")
    monkeypatch.setenv("JWT_AUDIENCE", "dreamplanner-api-test")
    app = create_app()
    app.dependency_overrides[get_session] = lambda: SimpleNamespace()
    refresh = "test-private-refresh-value"

    def grant_login(session: Session, email: str, password: str, codec) -> SessionGrant:
        sid = uuid4()
        return SessionGrant(
            7,
            sid,
            refresh,
            datetime.now(UTC) + timedelta(hours=24),
            codec.issue(7, sid),
        )

    monkeypatch.setattr(auth_router, "login", grant_login)
    with TestClient(app, base_url="http://localhost:8000") as client:
        missing_origin = client.post(
            LOGIN, json={"email": "user@example.com", "password": "private-password"}
        )
        assert missing_origin.status_code == 403
        assert "set-cookie" not in missing_origin.headers
        foreign_origin = client.post(
            LOGIN,
            headers={"Origin": "https://evil.example"},
            json={"email": "user@example.com", "password": "private-password"},
        )
        assert foreign_origin.status_code == 403
        accepted = client.post(
            LOGIN,
            headers={"Origin": ORIGIN},
            json={"email": "user@example.com", "password": "private-password"},
        )
        assert accepted.status_code == 200
        assert set(accepted.json()) == {"access_token", "token_type", "expires_in"}
        assert refresh not in accepted.text
        cookie = accepted.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "Secure" not in cookie
        assert "SameSite=lax" in cookie
        assert "Path=/api/v1/auth/sessions/current" in cookie
        assert accepted.headers["cache-control"] == "no-store"
        spec = client.get("/openapi.json").json()
        problem = spec["paths"][LOGIN]["post"]["responses"]["429"]["content"]
        assert "application/problem+json" in problem
        assert "private-password" not in repr(spec)


def test_secret_values_are_redacted_from_validation_errors(limited_app) -> None:
    """Pydantic input and context must never appear in HTTP error details."""
    response = _post(
        limited_app,
        LOGIN,
        "198.51.100.70",
        json={"email": "user@example.com", "password": ["private-password"]},
    )
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert "private-password" not in response.text
    assert all(
        set(error) == {"location", "code", "message"}
        for error in response.json()["errors"]
    )


def test_known_and_unknown_subjects_share_error_shapes(limited_app) -> None:
    """Do not reveal account or token lookup results in public failures."""
    known_refresh = _post(
        limited_app,
        REFRESH,
        "198.51.100.80",
        headers={"Cookie": "dreamplanner_refresh=known"},
    )
    unknown_refresh = _post(
        limited_app,
        REFRESH,
        "198.51.100.80",
        headers={"Cookie": "dreamplanner_refresh=unknown"},
    )
    assert known_refresh.status_code == unknown_refresh.status_code == 401
    assert known_refresh.json() == unknown_refresh.json()
    known_activation = _post(
        limited_app,
        ACTIVATE,
        "198.51.100.81",
        json={"token": "known", "new_password": "private-password"},
    )
    unknown_activation = _post(
        limited_app,
        ACTIVATE,
        "198.51.100.81",
        json={"token": "unknown", "new_password": "private-password"},
    )
    assert known_activation.status_code == unknown_activation.status_code == 401
    assert known_activation.json() == unknown_activation.json()


def test_approved_memory_moving_window_is_explicit(limited_app) -> None:
    """Keep the local MVP's selected limiter backend and strategy visible."""
    limiter = limited_app.state.auth_rate_limits.limiter.limiter
    assert isinstance(limiter, MovingWindowRateLimiter)
    assert isinstance(limiter.storage, MemoryStorage)
