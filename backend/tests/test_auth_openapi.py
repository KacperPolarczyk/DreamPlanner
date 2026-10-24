"""OpenAPI contracts for authentication, authorization, and problem responses."""

from fastapi.testclient import TestClient

from src.main import app


def test_openapi_keeps_bearer_jwt_on_protected_routes_only() -> None:
    """Retain Swagger authorization without securing public token operations."""
    schema = app.openapi()
    assert "security" not in schema
    assert schema["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": (
            "Token dostępu JWT w nagłówku Authorization ze schematem Bearer."
        ),
    }
    for path, method in (
        ("/api/v1/auth/sessions/current", "delete"),
        ("/api/v1/auth/sessions", "delete"),
        ("/api/v1/users/me", "get"),
        ("/api/v1/users/me/password-change", "post"),
    ):
        assert schema["paths"][path][method]["security"] == [{"BearerAuth": []}]
    for path, method in (
        ("/api/v1/auth/sessions", "post"),
        ("/api/v1/auth/sessions/current/refresh", "post"),
        ("/api/v1/auth/activations", "post"),
        ("/api/v1/auth/reactivations", "post"),
        ("/api/v1/health", "get"),
    ):
        assert "security" not in schema["paths"][path][method]


def test_openapi_describes_admin_role_for_every_admin_operation() -> None:
    """Bearer alone does not document the live database role requirement."""
    paths = app.openapi()["paths"]
    for path, method in (
        ("/api/v1/users", "get"),
        ("/api/v1/users", "post"),
        ("/api/v1/users/{user_id}", "get"),
        ("/api/v1/users/{user_id}/role", "put"),
        ("/api/v1/users/{user_id}/deactivation", "post"),
        ("/api/v1/users/{user_id}/reactivation", "post"),
        ("/api/v1/users/{user_id}/activation-tokens", "post"),
    ):
        operation = paths[path][method]
        assert operation["security"] == [{"BearerAuth": []}]
        assert "`admin`" in operation["description"]
        assert "401" in operation["responses"]
        assert "403" in operation["responses"]


def test_openapi_validation_problem_matches_http_shape() -> None:
    """Document field errors on 422 without adding them to other problems."""
    schema = app.openapi()
    for path in schema["paths"].values():
        for operation in path.values():
            responses = operation.get("responses", {})
            if "422" not in responses:
                continue
            problem = responses["422"]["content"]["application/problem+json"]["schema"]
            if "oneOf" in problem:
                problem = problem["oneOf"][0]
            assert problem["properties"]["type"]["const"] == (
                "urn:dreamplanner:problem:validation-error"
            )
            assert problem["properties"]["status"]["const"] == 422
            assert set(problem["required"]) >= {"type", "title", "status", "detail"}
            errors = problem["properties"]["errors"]
            assert errors["type"] == "array"
            assert set(errors["items"]["required"]) == {
                "location",
                "code",
                "message",
            }
            assert errors["items"]["properties"]["location"]["items"]["type"] == (
                "string"
            )
            assert "$ref" not in errors["items"]
    ordinary = schema["paths"]["/api/v1/auth/sessions"]["post"]["responses"]["401"][
        "content"
    ]["application/problem+json"]["schema"]
    assert "errors" not in ordinary["properties"]

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/sessions",
            headers={"Origin": "http://localhost:5173"},
            json={"email": "user@example.com", "password": ["invalid"]},
        )
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert set(response.json()) == {"type", "title", "status", "detail", "errors"}
    assert all(
        set(error) == {"location", "code", "message"}
        for error in response.json()["errors"]
    )


def test_openapi_origin_requirements_match_statuses() -> None:
    """Cookie routes and token routes advertise their actual protection."""
    paths = app.openapi()["paths"]
    for path, method in (
        ("/api/v1/auth/sessions", "post"),
        ("/api/v1/auth/sessions/current/refresh", "post"),
        ("/api/v1/auth/sessions/current", "delete"),
        ("/api/v1/auth/sessions", "delete"),
    ):
        operation = paths[path][method]
        assert "Origin" in operation["description"]
        assert "403" in operation["responses"]
    for path, method in (
        ("/api/v1/auth/activations", "post"),
        ("/api/v1/auth/reactivations", "post"),
        ("/api/v1/users/me/password-change", "post"),
    ):
        operation = paths[path][method]
        assert "Origin nie jest wymagany" in operation["description"]
        assert "422" in operation["responses"]
        assert "403" in operation["responses"]
    assert (
        "HTTPS"
        in paths["/api/v1/auth/reactivations"]["post"]["responses"]["403"][
            "description"
        ]
    )
