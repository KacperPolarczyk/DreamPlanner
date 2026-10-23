"""OpenAPI contracts for access token authentication."""

from src.main import app


def test_openapi_declares_bearer_jwt_only_for_logout() -> None:
    """Document the protected operation without marking public routes as secured."""
    schema = app.openapi()
    assert "security" not in schema
    assert schema["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
    }
    assert schema["paths"]["/api/v1/auth/sessions/current"]["delete"]["security"] == [
        {"BearerAuth": []}
    ]
    for path, method in (
        ("/api/v1/auth/sessions", "post"),
        ("/api/v1/auth/sessions/current/refresh", "post"),
        ("/api/v1/auth/activations", "post"),
        ("/api/v1/health", "get"),
    ):
        assert "security" not in schema["paths"][path][method]
