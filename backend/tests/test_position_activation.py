"""SQLite HTTP checks for position deactivation and reactivation."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from src.positions.models import Position
from src.positions.service import set_position_active
from tests.test_position_creation import PATH, _headers, _positions

pytest_plugins = ["tests.test_position_creation"]

OPERATIONS = [
    ("deactivation", "10", False, "Kitchen"),
    ("reactivation", "11", True, "Reception"),
]


def _post(client: TestClient, token: str, operation: str, position_id: str):
    return client.post(f"{PATH}/{position_id}/{operation}", headers=_headers(token))


@pytest.mark.parametrize(("operation", "position_id", "active", "name"), OPERATIONS)
def test_activity_change_persists_and_is_idempotent(
    create_api, operation, position_id, active, name
) -> None:
    app, token, _, engine = create_api
    updates = []

    def record_update(mapper, connection, target) -> None:
        updates.append(target.id)

    event.listen(Position, "before_update", record_update)
    try:
        with TestClient(app) as client:
            first = _post(client, token, operation, position_id)
            second = _post(client, token, operation, position_id)
            detail = client.get(f"{PATH}/{position_id}", headers=_headers(token))
            listing = client.get(PATH, headers=_headers(token))
    finally:
        event.remove(Position, "before_update", record_update)

    expected = {"id": position_id, "name": name, "is_active": active}
    assert first.status_code == second.status_code == detail.status_code == 200
    assert first.json() == second.json() == detail.json() == expected
    assert first.headers["cache-control"] == "no-store"
    assert isinstance(first.json()["id"], str)
    assert updates == [int(position_id)]
    assert [(row.id, row.name, row.is_active) for row in _positions(engine)] == [
        (10, "Kitchen", active if position_id == "10" else True),
        (11, "Reception", active if position_id == "11" else False),
    ]
    assert expected in listing.json()["items"]


@pytest.mark.parametrize("operation", ["deactivation", "reactivation"])
@pytest.mark.parametrize("role", ["manager", "employee"])
def test_non_admin_role_is_forbidden_before_lookup(create_api, operation, role) -> None:
    app, token, actor, engine = create_api
    actor.role.code = role
    with TestClient(app) as client:
        responses = [
            _post(client, token, operation, position_id)
            for position_id in ("10", "999")
        ]
    assert [response.status_code for response in responses] == [403, 403]
    assert all(
        response.headers["content-type"] == "application/problem+json"
        for response in responses
    )
    assert [(row.name, row.is_active) for row in _positions(engine)] == [
        ("Kitchen", True),
        ("Reception", False),
    ]


@pytest.mark.parametrize("operation", ["deactivation", "reactivation"])
def test_no_token_is_401(create_api, operation) -> None:
    app, _, _, _ = create_api
    with TestClient(app) as client:
        response = client.post(f"{PATH}/10/{operation}")
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("operation", ["deactivation", "reactivation"])
@pytest.mark.parametrize("position_id", ["0", "999"])
def test_missing_position_is_404(create_api, operation, position_id) -> None:
    app, token, _, _ = create_api
    with TestClient(app) as client:
        response = _post(client, token, operation, position_id)
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "about:blank"


@pytest.mark.parametrize("operation", ["deactivation", "reactivation"])
@pytest.mark.parametrize("position_id", ["invalid", "-1", "9223372036854775808"])
def test_invalid_position_id_is_422(create_api, operation, position_id) -> None:
    app, token, _, _ = create_api
    with TestClient(app) as client:
        response = _post(client, token, operation, position_id)
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"


@pytest.mark.parametrize("operation", ["deactivation", "reactivation"])
@pytest.mark.parametrize("content", [b"{}", b"null", b" ", b"text"])
def test_nonempty_body_is_422(create_api, operation, content) -> None:
    app, token, _, engine = create_api
    with TestClient(app) as client:
        response = client.post(
            f"{PATH}/10/{operation}", content=content, headers=_headers(token)
        )
    assert response.status_code == 422
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert _positions(engine)[0].is_active is True


def test_manager_can_read_deactivated_position(create_api) -> None:
    app, token, actor, _ = create_api
    with TestClient(app) as client:
        assert _post(client, token, "deactivation", "10").status_code == 200
        actor.role.code = "manager"
        detail = client.get(f"{PATH}/10", headers=_headers(token))
        listing = client.get(PATH, headers=_headers(token))
    assert detail.status_code == listing.status_code == 200
    assert detail.json() == {"id": "10", "name": "Kitchen", "is_active": False}
    assert detail.json() in listing.json()["items"]


def test_activity_change_preserves_existing_links(create_api) -> None:
    app, token, _, engine = create_api
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE shift_requirements "
            "(id INTEGER PRIMARY KEY, position_id BIGINT NOT NULL "
            "REFERENCES positions(id))"
        )
        connection.exec_driver_sql(
            "CREATE TABLE shift_assignments "
            "(id INTEGER PRIMARY KEY, shift_requirement_id INTEGER NOT NULL "
            "REFERENCES shift_requirements(id))"
        )
        connection.exec_driver_sql(
            "INSERT INTO shift_requirements (id, position_id) VALUES (1, 10)"
        )
        connection.exec_driver_sql(
            "INSERT INTO shift_assignments (id, shift_requirement_id) VALUES (1, 1)"
        )
    with TestClient(app) as client:
        assert _post(client, token, "deactivation", "10").status_code == 200
        assert _post(client, token, "reactivation", "10").status_code == 200
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT position_id FROM shift_requirements WHERE id = 1"
            ).scalar_one()
            == 10
        )
        assert (
            connection.exec_driver_sql(
                "SELECT shift_requirement_id FROM shift_assignments WHERE id = 1"
            ).scalar_one()
            == 1
        )


def test_failed_activity_write_rolls_back(create_api) -> None:
    _, _, _, engine = create_api

    def fail_update(mapper, connection, target) -> None:
        raise RuntimeError("simulated write failure")

    event.listen(Position, "before_update", fail_update)
    try:
        with Session(engine) as session:
            with pytest.raises(RuntimeError, match="simulated write failure"):
                set_position_active(session, 10, is_active=False)
            assert not session.in_transaction()
    finally:
        event.remove(Position, "before_update", fail_update)
    with Session(engine) as session:
        assert session.scalar(select(Position.is_active).where(Position.id == 10))


def test_openapi_documents_both_activity_operations(create_api) -> None:
    app, _, _, _ = create_api
    for operation in ("deactivation", "reactivation"):
        spec = app.openapi()["paths"][f"{PATH}/{{position_id}}/{operation}"]["post"]
        assert spec["security"] == [{"BearerAuth": []}]
        assert "`admin`" in spec["description"]
        assert "requestBody" not in spec
        assert {"200", "401", "403", "404", "422"} <= set(spec["responses"])
        assert spec["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/PositionResponse"
        }
        for status in ("401", "403", "404", "422"):
            assert "application/problem+json" in spec["responses"][status]["content"]
