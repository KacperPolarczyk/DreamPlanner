"""Pin Uvicorn proxy handling and its effect on authentication limits."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from uvicorn import Config
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from src.auth import router as auth_router
from src.auth.session_service import InvalidCredentials
from src.core.database import get_session
from src.main import create_app

TRUSTED_V4 = "192.0.2.10"
TRUSTED_V6 = "2001:db8::10"
UNTRUSTED_V4 = "198.51.100.10"
LOGIN = "/api/v1/auth/sessions"


def _scope_after_proxy(
    *,
    peer: str,
    headers: list[tuple[bytes, bytes]],
    trusted_hosts: list[str] | None = None,
) -> dict:
    """Run the installed Uvicorn middleware against one HTTP ASGI scope."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": (peer, 40000),
        "server": ("127.0.0.1", 8000),
    }
    observed = {}

    async def capture(inner_scope, receive, send):
        observed.update(inner_scope)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    middleware = ProxyHeadersMiddleware(
        capture, trusted_hosts=trusted_hosts or [TRUSTED_V4, TRUSTED_V6]
    )
    asyncio.run(middleware(scope, receive, send))
    return observed


@pytest.mark.parametrize(
    ("peer", "headers", "expected_scheme", "expected_client"),
    [
        (TRUSTED_V4, [(b"x-forwarded-proto", b"https")], "https", (TRUSTED_V4, 40000)),
        (
            UNTRUSTED_V4,
            [(b"x-forwarded-proto", b"https")],
            "http",
            (UNTRUSTED_V4, 40000),
        ),
        (
            TRUSTED_V4,
            [(b"x-forwarded-for", b"203.0.113.4")],
            "http",
            ("203.0.113.4", 0),
        ),
        (
            UNTRUSTED_V4,
            [(b"x-forwarded-for", b"203.0.113.4")],
            "http",
            (UNTRUSTED_V4, 40000),
        ),
        (
            TRUSTED_V4,
            [(b"x-forwarded-for", b"203.0.113.4")],
            "http",
            ("203.0.113.4", 0),
        ),
        (TRUSTED_V4, [(b"x-forwarded-proto", b"https")], "https", (TRUSTED_V4, 40000)),
        (
            TRUSTED_V4,
            [(b"x-forwarded-for", b"198.51.100.1, 203.0.113.4")],
            "http",
            ("203.0.113.4", 0),
        ),
        (
            TRUSTED_V4,
            [(b"x-forwarded-for", b"198.51.100.1, 192.0.2.10")],
            "http",
            ("198.51.100.1", 0),
        ),
        (
            TRUSTED_V6,
            [(b"x-forwarded-proto", b"https"), (b"x-forwarded-for", b"2001:db8::42")],
            "https",
            ("2001:db8::42", 0),
        ),
        (
            TRUSTED_V4,
            [(b"forwarded", b"for=203.0.113.4;proto=https")],
            "http",
            (TRUSTED_V4, 40000),
        ),
        (TRUSTED_V4, [(b"x-forwarded-proto", b"HTTPS")], "http", (TRUSTED_V4, 40000)),
        (TRUSTED_V4, [(b"x-forwarded-for", b"")], "http", (TRUSTED_V4, 40000)),
        (TRUSTED_V4, [(b"x-forwarded-for", b"not-an-ip")], "http", ("not-an-ip", 0)),
    ],
    ids=[
        "trusted-proto",
        "untrusted-proto",
        "trusted-for-ipv4",
        "untrusted-for",
        "missing-proto",
        "missing-for",
        "multi-for-rightmost-untrusted",
        "multi-for-skip-trusted",
        "ipv6",
        "forwarded-is-ignored",
        "invalid-proto-is-ignored",
        "empty-for-is-ignored",
        "malformed-for-is-passed-through",
    ],
)
def test_uvicorn_proxy_scope(peer, headers, expected_scheme, expected_client) -> None:
    """Check the actual scheme and client tuple produced by installed Uvicorn."""
    scope = _scope_after_proxy(peer=peer, headers=headers)
    assert scope["scheme"] == expected_scheme
    assert scope["client"] == expected_client


@pytest.fixture
def auth_app(monkeypatch: pytest.MonkeyPatch):
    """Use real auth routes and counters without a database connection."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("REFRESH_COOKIE_SECURE", "true")
    monkeypatch.setenv("CORS_ORIGINS", '["https://app.example.com"]')
    app = create_app()
    app.dependency_overrides[get_session] = lambda: SimpleNamespace()

    def deny_login(session: Session, email: str, password: str, codec: object) -> None:
        raise InvalidCredentials("Invalid credentials.")

    monkeypatch.setattr(auth_router, "login", deny_login)
    return app


def _login(client: TestClient, index: int, headers: dict[str, str] | None = None):
    """Use a distinct identity so the IP limit is the decisive counter."""
    return client.post(
        LOGIN,
        headers={"Origin": "https://app.example.com", **(headers or {})},
        json={"email": f"person{index}@example.com", "password": "wrong"},
    )


def test_trusted_https_reaches_auth_and_http_is_rejected(auth_app) -> None:
    """The production guard reads the effective Uvicorn scheme."""
    middleware = ProxyHeadersMiddleware(auth_app, trusted_hosts=[TRUSTED_V4])
    with TestClient(middleware, client=(TRUSTED_V4, 40000)) as client:
        assert _login(client, 1, {"X-Forwarded-Proto": "https"}).status_code == 401
        assert _login(client, 2).status_code == 403
    with TestClient(middleware, client=(UNTRUSTED_V4, 40000)) as client:
        assert _login(client, 3, {"X-Forwarded-Proto": "https"}).status_code == 403


def test_trusted_forwarded_client_uses_one_ip_limit(auth_app) -> None:
    """A forwarded client keeps its IP counter across distinct trusted peers."""
    middleware = ProxyHeadersMiddleware(
        auth_app, trusted_hosts=[TRUSTED_V4, "192.0.2.11"]
    )
    for index in range(20):
        peer = TRUSTED_V4 if index % 2 == 0 else "192.0.2.11"
        with TestClient(middleware, client=(peer, 40000)) as client:
            response = _login(
                client,
                index,
                {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.8"},
            )
        assert response.status_code == 401
    with TestClient(middleware, client=(TRUSTED_V4, 40000)) as client:
        blocked = _login(
            client,
            20,
            {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.8"},
        )
        other = _login(
            client,
            21,
            {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"},
        )
    assert blocked.status_code == 429
    assert other.status_code == 401


def test_untrusted_spoofed_for_cannot_rotate_ip_limit(auth_app) -> None:
    """Changing XFF does not evade the socket peer's IP counter."""
    middleware = ProxyHeadersMiddleware(auth_app, trusted_hosts=[TRUSTED_V4])
    with TestClient(middleware, client=(UNTRUSTED_V4, 40000)) as client:
        for index in range(20):
            response = _login(
                client,
                index,
                {"X-Forwarded-For": f"203.0.113.{index + 1}"},
            )
            assert response.status_code == 403
        blocked = _login(client, 20, {"X-Forwarded-For": "203.0.113.250"})
    assert blocked.status_code == 429


def test_local_http_ignores_proxy_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct local HTTP continues to ignore client-provided proxy headers."""
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("REFRESH_COOKIE_SECURE", "false")
    monkeypatch.setenv("CORS_ORIGINS", '["http://localhost:5173"]')
    app = create_app()
    app.dependency_overrides[get_session] = lambda: SimpleNamespace()

    def deny_login(session: Session, email: str, password: str, codec: object) -> None:
        raise InvalidCredentials("Invalid credentials.")

    monkeypatch.setattr(auth_router, "login", deny_login)
    observed_scopes = []

    async def capture_scope(scope, receive, send):
        if scope["type"] == "http":
            observed_scopes.append((scope["scheme"], scope["client"]))
        await app(scope, receive, send)

    config = Config(capture_scope, proxy_headers=False)
    config.load()
    with TestClient(
        config.loaded_app,
        base_url="http://localhost:8000",
        client=(UNTRUSTED_V4, 40000),
    ) as client:
        for index in range(20):
            response = client.post(
                LOGIN,
                headers={
                    "Origin": "http://localhost:5173",
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-For": f"203.0.113.{index + 1}",
                },
                json={"email": f"person{index}@example.com", "password": "wrong"},
            )
            assert response.status_code == 401
        blocked = client.post(
            LOGIN,
            headers={
                "Origin": "http://localhost:5173",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-For": "203.0.113.250",
            },
            json={"email": "person20@example.com", "password": "wrong"},
        )
    assert blocked.status_code == 429
    assert observed_scopes == [("http", (UNTRUSTED_V4, 40000))] * 21
