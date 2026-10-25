"""HTTP contract tests for application-wide Problem Details responses."""

import logging

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.auth.dependencies import AuthHttpError
from src.auth.rate_limits import RateLimited
from src.main import create_app


def _assert_problem(response, status: int, kind: str = "about:blank") -> dict:
    """Check the shared RFC 9457 shape and HTTP metadata."""
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"type", "title", "status", "detail"}
    assert body["type"] == kind
    assert body["status"] == status
    assert isinstance(body["title"], str) and body["title"]
    assert isinstance(body["detail"], str) and body["detail"]
    return body


def test_missing_route_and_disallowed_method_use_problem_details() -> None:
    """Framework-generated 404 and 405 share the application error format."""
    with TestClient(create_app()) as client:
        missing = client.get("/api/v1/no-such-endpoint")
        disallowed = client.post("/api/v1/health")
        healthy = client.get("/api/v1/health")

    assert _assert_problem(missing, 404)["title"] == "Nie znaleziono"
    assert _assert_problem(disallowed, 405)["title"] == "Metoda niedozwolona"
    assert disallowed.headers["allow"] == "GET"
    assert healthy.status_code == 200
    assert healthy.json() == {"status": "ok"}


def test_missing_jwt_signing_configuration_returns_documented_503() -> None:
    """A protected request receives the safe conditional service problem."""
    app = create_app()
    app.state.jwt_codec = None
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/users/me", headers={"Authorization": "Bearer test-token"}
        )
    assert _assert_problem(response, 503) == {
        "type": "about:blank",
        "title": "Usługa niedostępna",
        "status": 503,
        "detail": "Uwierzytelnianie jest niedostępne.",
    }


def test_unexpected_error_is_logged_but_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A server failure returns only fixed text while retaining a traceback."""
    app = create_app()
    secret = "password=secret token=private SELECT * FROM users"

    @app.get("/test/failure")
    def fail() -> None:
        raise RuntimeError(secret)

    with caplog.at_level(logging.ERROR, logger="src.main"):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/test/failure?token=private")

    assert _assert_problem(response, 500) == {
        "type": "about:blank",
        "title": "Internal Server Error",
        "status": 500,
        "detail": "Wystąpił błąd serwera.",
    }
    assert secret not in response.text
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text
    assert any(
        record.exc_info and secret in str(record.exc_info[1])
        for record in caplog.records
    )


def test_http_exception_preserves_custom_headers_and_redacts_server_detail() -> None:
    """Framework exception metadata survives without exposing a 500 detail."""
    app = create_app()

    @app.get("/test/http-error")
    def fail_http() -> None:
        raise HTTPException(
            status_code=500,
            detail="private SQL and token",
            headers={"Retry-After": "30", "X-Request-Status": "retry"},
        )

    with TestClient(app) as client:
        response = client.get("/test/http-error")

    _assert_problem(response, 500)
    assert response.headers["retry-after"] == "30"
    assert response.headers["x-request-status"] == "retry"
    assert "private SQL and token" not in response.text


def test_existing_auth_validation_and_limit_contracts() -> None:
    """Specific handlers retain their types, extensions, and required headers."""
    app = create_app()

    @app.get("/test/auth")
    def auth() -> None:
        raise AuthHttpError(
            401,
            "about:blank",
            "Brak uwierzytelnienia",
            "Nieprawidłowe uwierzytelnienie.",
            bearer=True,
        )

    @app.get("/test/forbidden")
    def forbidden() -> None:
        raise AuthHttpError(403, "about:blank", "Odmowa dostępu", "Brak uprawnienia.")

    @app.get("/test/validation")
    def validation(number: int) -> dict[str, int]:
        return {"number": number}

    @app.get("/test/limited")
    def limited() -> None:
        raise RateLimited({"Retry-After": "12", "X-RateLimit-Remaining": "0"})

    with TestClient(app) as client:
        unauthenticated = client.get("/test/auth")
        denied = client.get("/test/forbidden")
        invalid = client.get("/test/validation?number=invalid")
        throttled = client.get("/test/limited")

    _assert_problem(unauthenticated, 401)
    assert unauthenticated.headers["www-authenticate"] == "Bearer"
    _assert_problem(denied, 403)
    assert "www-authenticate" not in denied.headers
    assert invalid.status_code == 422
    assert invalid.headers["content-type"] == "application/problem+json"
    assert invalid.headers["cache-control"] == "no-store"
    invalid_body = invalid.json()
    assert set(invalid_body) == {"type", "title", "status", "detail", "errors"}
    assert invalid_body["type"] == "urn:dreamplanner:problem:validation-error"
    assert invalid_body["status"] == 422
    assert invalid_body["errors"] == [
        {
            "location": ["query", "number"],
            "code": "int_parsing",
            "message": "Nieprawidłowa wartość.",
        }
    ]
    _assert_problem(throttled, 429, "urn:dreamplanner:problem:rate-limited")
    assert throttled.headers["retry-after"] == "12"
    assert throttled.headers["x-ratelimit-remaining"] == "0"
