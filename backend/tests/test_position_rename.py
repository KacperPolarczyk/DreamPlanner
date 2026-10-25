"""HTTP contract checks for administrator position renaming on SQLite."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from src.positions.models import Position
from src.positions.repository import PositionRepository
from tests.test_position_creation import (
    LARGE_ID,
    PATH,
    _headers,
    _positions,
)

pytest_plugins = ["tests.test_position_creation"]


def _patch(client: TestClient, token: str, position_id: str, name: str):
    return client.patch(
        f"{PATH}/{position_id}", json={"name": name}, headers=_headers(token)
    )


def test_admin_renames_and_persists_position(create_api) -> None:
    app, token, _, engine = create_api
    with TestClient(app) as client:
        response = _patch(client, token, "10", "  Lead Kitchen  ")
    assert response.status_code == 200
    assert response.json() == {
        "id": "10",
        "name": "Lead Kitchen",
        "is_active": True,
    }
    assert isinstance(response.json()["id"], str)
    assert response.headers["cache-control"] == "no-store"
    assert [(row.name, row.is_active) for row in _positions(engine)] == [
        ("Lead Kitchen", True),
        ("Reception", False),
    ]


@pytest.mark.parametrize("role", ["manager", "employee"])
def test_non_admin_cannot_rename_even_missing_position(create_api, role) -> None:
    app, token, actor, engine = create_api
    actor.role.code = role
    with TestClient(app) as client:
        responses = [_patch(client, token, path, "New") for path in ("10", "999")]
    assert [response.status_code for response in responses] == [403, 403]
    assert all(
        response.headers["content-type"] == "application/problem+json"
        for response in responses
    )
    assert _positions(engine)[0].name == "Kitchen"


def test_missing_token_is_401(create_api) -> None:
    app, _, _, engine = create_api
    with TestClient(app) as client:
        response = client.patch(f"{PATH}/10", json={"name": "New"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["content-type"] == "application/problem+json"
    assert _positions(engine)[0].name == "Kitchen"


@pytest.mark.parametrize("name", ["Kitchen", "  Kitchen  ", "A" * 120])
def test_same_name_and_length_boundary_succeed(create_api, name) -> None:
    app, token, _, engine = create_api
    with TestClient(app) as client:
        response = _patch(client, token, "10", name)
    assert response.status_code == 200
    assert response.json()["name"] == name.strip()
    assert _positions(engine)[0].name == name.strip()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": "A" * 121},
        {"name": None},
        {"name": 1},
        {"name": ["New"]},
        {"name": "New", "id": "10"},
        {"name": "New", "is_active": False},
        {"name": "New", "extra": True},
        {"name": "New\nName"},
    ],
)
def test_invalid_body_returns_422(create_api, payload) -> None:
    app, token, _, engine = create_api
    with TestClient(app) as client:
        response = client.patch(f"{PATH}/10", json=payload, headers=_headers(token))
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert response.json()["errors"]
    assert _positions(engine)[0].name == "Kitchen"


def test_inactive_position_can_be_renamed_without_activation(create_api) -> None:
    app, token, _, engine = create_api
    with TestClient(app) as client:
        response = _patch(client, token, "11", "  Front Desk  ")
    assert response.status_code == 200
    assert response.json() == {"id": "11", "name": "Front Desk", "is_active": False}
    assert [(row.name, row.is_active) for row in _positions(engine)][1] == (
        "Front Desk",
        False,
    )


@pytest.mark.parametrize("name", ["Reception", "  Kitchen  "])
def test_active_and_inactive_name_conflicts_are_safe(create_api, name) -> None:
    app, token, _, engine = create_api
    position_id = "10" if name == "Reception" else "11"
    with TestClient(app) as client:
        response = _patch(client, token, position_id, name)
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "urn:dreamplanner:problem:position-name-conflict",
        "title": "Nazwa stanowiska jest już zajęta",
        "status": 409,
        "detail": "Stanowisko o tej nazwie już istnieje.",
    }
    assert "UNIQUE" not in response.text
    assert [row.name for row in _positions(engine)] == ["Kitchen", "Reception"]


def test_case_difference_and_canonical_bigint_id(create_api) -> None:
    app, token, _, engine = create_api
    with Session(engine) as session:
        session.add(Position(id=LARGE_ID, name="Large", is_active=False))
        session.commit()
    with TestClient(app) as client:
        lower = _patch(client, token, "10", "kitchen")
        large = _patch(client, token, f"00{LARGE_ID}", "Larger")
    assert lower.status_code == large.status_code == 200
    assert lower.json()["name"] == "kitchen"
    assert large.json() == {"id": str(LARGE_ID), "name": "Larger", "is_active": False}
    assert [(row.id, row.name) for row in _positions(engine)] == [
        (10, "kitchen"),
        (11, "Reception"),
        (LARGE_ID, "Larger"),
    ]


@pytest.mark.parametrize("position_id", ["missing", "-1", "9223372036854775808"])
def test_invalid_id_returns_422(create_api, position_id) -> None:
    app, token, _, _ = create_api
    with TestClient(app) as client:
        response = _patch(client, token, position_id, "New")
    assert response.status_code == 422
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"


@pytest.mark.parametrize("position_id", ["0", "999"])
def test_missing_position_returns_404(create_api, position_id) -> None:
    app, token, _, _ = create_api
    with TestClient(app) as client:
        response = _patch(client, token, position_id, "New")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "about:blank"


def test_invalid_json_returns_400(create_api) -> None:
    app, token, _, _ = create_api
    with TestClient(app) as client:
        response = client.patch(
            f"{PATH}/10",
            content='{"name":',
            headers={**_headers(token), "Content-Type": "application/json"},
        )
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"


def test_repository_uses_postgres_row_lock() -> None:
    class RecordingSession:
        statement = None

        def scalar(self, statement):
            self.statement = statement
            return None

    session = RecordingSession()
    assert PositionRepository(session).get_by_id_for_update(10) is None
    statement = session.statement
    assert "FOR UPDATE" in str(statement.compile(dialect=postgresql.dialect()))
    assert statement.get_execution_options()["populate_existing"] is True


def test_openapi_documents_admin_rename(create_api) -> None:
    app, _, _, _ = create_api
    operation = app.openapi()["paths"][f"{PATH}/{{position_id}}"]["patch"]
    assert operation["security"] == [{"BearerAuth": []}]
    assert "`admin`" in operation["description"]
    assert {"200", "400", "401", "403", "404", "409", "422"} <= set(
        operation["responses"]
    )
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RenamePositionInput"
    }
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PositionResponse"
    }
    for status in ("400", "401", "403", "404", "409", "422"):
        assert "application/problem+json" in operation["responses"][status]["content"]
