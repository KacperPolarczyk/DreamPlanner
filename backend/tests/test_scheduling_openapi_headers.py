"""OpenAPI documents the headers sent by protected scheduling endpoints."""

import pytest

from src.main import create_app

OPERATIONS = [
    ("/api/v1/schedules", "get", "200"),
    ("/api/v1/schedules", "post", "201"),
    ("/api/v1/schedules/{schedule_id}", "get", "200"),
    ("/api/v1/schedules/{schedule_id}", "delete", "204"),
    ("/api/v1/schedules/{schedule_id}/collection-opening", "post", "200"),
    ("/api/v1/schedules/{schedule_id}/collection-closing", "post", "200"),
    ("/api/v1/schedules/{schedule_id}/closing", "post", "200"),
    ("/api/v1/schedules/{schedule_id}/publication", "post", "200"),
    ("/api/v1/schedules/{schedule_id}/shift-requirements", "get", "200"),
    ("/api/v1/schedules/{schedule_id}/shift-requirements", "post", "201"),
    (
        "/api/v1/schedules/{schedule_id}/shift-requirements/{requirement_id}",
        "get",
        "200",
    ),
    (
        "/api/v1/schedules/{schedule_id}/shift-requirements/{requirement_id}",
        "patch",
        "200",
    ),
    (
        "/api/v1/schedules/{schedule_id}/shift-requirements/{requirement_id}",
        "delete",
        "204",
    ),
    ("/api/v1/schedules/{schedule_id}/shift-assignments", "get", "200"),
    ("/api/v1/schedules/{schedule_id}/shift-assignments", "post", "201"),
    (
        "/api/v1/schedules/{schedule_id}/shift-assignments/{assignment_id}",
        "get",
        "200",
    ),
    (
        "/api/v1/schedules/{schedule_id}/shift-assignments/{assignment_id}",
        "delete",
        "204",
    ),
    ("/api/v1/me/shifts", "get", "200"),
    ("/api/v1/me/shifts/{assignment_id}", "get", "200"),
]


@pytest.fixture(scope="module")
def openapi() -> dict:
    """Build the generated contract without starting the HTTP server."""
    return create_app().openapi()


@pytest.mark.parametrize(("path", "method", "success_code"), OPERATIONS)
def test_scheduling_response_headers_are_documented(
    openapi: dict, path: str, method: str, success_code: str
) -> None:
    """Keep no-store and creation Location visible to generated API clients."""
    responses = openapi["paths"][path][method]["responses"]
    success = responses[success_code]
    assert success["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
    if success_code == "201":
        assert success["headers"]["Location"]["schema"]["type"] == "string"
    for code, response in responses.items():
        if code in {"400", "401", "403", "404", "409", "422"}:
            assert response["headers"]["Cache-Control"]["schema"]["const"] == (
                "no-store"
            )
