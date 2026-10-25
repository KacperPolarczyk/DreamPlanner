"""Browser CORS behavior and environment configuration tests."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.core.config import Settings
from src.main import create_app


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:5173"])
@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_preflight_allows_local_origins(origin: str, method: str) -> None:
    client = TestClient(create_app())

    response = client.options(
        "/api/v1/health",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"
    assert response.headers["access-control-allow-methods"].split(", ") == [
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
    ]
    assert "authorization" in response.headers["access-control-allow-headers"].lower()
    assert "content-type" in response.headers["access-control-allow-headers"].lower()


def test_preflight_rejects_unlisted_origin() -> None:
    client = TestClient(create_app())

    response = client.options(
        "/api/v1/health",
        headers={
            "Origin": "https://unlisted.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers

    simple_response = client.get(
        "/api/v1/health", headers={"Origin": "https://unlisted.example.com"}
    )
    assert "access-control-allow-origin" not in simple_response.headers


def test_preflight_rejects_unneeded_method() -> None:
    client = TestClient(create_app())

    response = client.options(
        "/api/v1/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "TRACE",
        },
    )

    assert response.status_code == 400


def test_preflight_rejects_unlisted_header() -> None:
    client = TestClient(create_app())

    response = client.options(
        "/api/v1/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-unlisted",
        },
    )

    assert response.status_code == 400


def test_cors_origins_can_be_replaced_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    production_origin = "https://app.example.com"
    monkeypatch.setenv("CORS_ORIGINS", '["https://app.example.com"]')

    assert Settings().cors_origins == [production_origin]

    client = TestClient(create_app())
    response = client.options(
        "/api/v1/health",
        headers={
            "Origin": production_origin,
            "Access-Control-Request-Method": "GET",
        },
    )
    local_response = client.options(
        "/api/v1/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == production_origin
    assert local_response.status_code == 400


def test_cors_origins_reject_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", '["*"]')

    with pytest.raises(ValidationError, match="CORS origins must be explicit"):
        Settings()
