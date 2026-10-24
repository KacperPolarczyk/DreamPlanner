"""HTTP and transaction checks for safe shift requirement deletion."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session
from test_shift_requirement_reads import (
    LARGE_ID,
    SCHEDULE_ID,
    _detail_path,
    _headers,
)
from test_shift_requirement_reads import (
    shift_requirements_api as shared_shift_requirements_api,
)

from src.assignments.models import ShiftAssignment
from src.schedules.models import ShiftRequirement

shift_requirements_api = shared_shift_requirements_api


def _delete(client, token, schedule_id=SCHEDULE_ID, requirement_id=LARGE_ID):
    return client.delete(
        _detail_path(schedule_id, requirement_id), headers=_headers(token)
    )


def _add_assignments(engine, requirement_id=LARGE_ID, count=1):
    with Session(engine) as session:
        session.add_all(
            [
                ShiftAssignment(
                    id=100 + index,
                    shift_requirement_id=requirement_id,
                    user_id=1 + index,
                    assigned_by=2,
                )
                for index in range(count)
            ]
        )
        session.commit()


def test_manager_deletes_only_requested_requirement_and_returns_empty_204(
    shift_requirements_api,
):
    app, token, actor, engine = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        response = _delete(client, token)
        detail = client.get(
            _detail_path(SCHEDULE_ID, LARGE_ID), headers=_headers(token)
        )
    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert "location" not in response.headers
    assert detail.status_code == 404
    with Session(engine) as session:
        assert session.get(ShiftRequirement, LARGE_ID) is None
        assert session.get(ShiftRequirement, 124) is not None
        assert session.get(ShiftRequirement, 125) is not None


@pytest.mark.parametrize("role", ["admin", "employee"])
def test_only_manager_can_delete_before_resource_lookup(shift_requirements_api, role):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = role
    with TestClient(app) as client:
        response = _delete(client, token, 999999, 999999)
    assert response.status_code == 403
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"


def test_missing_token_is_401_before_resource_lookup(shift_requirements_api):
    app, _, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        response = client.delete(_detail_path(999999, 999999))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    ("schedule_id", "requirement_id", "expected"),
    [
        (SCHEDULE_ID, LARGE_ID, 204),
        (46, 127, 409),
        (47, 201, 409),
        (48, 202, 409),
        (49, 203, 409),
    ],
)
def test_delete_requires_draft_schedule(
    shift_requirements_api, schedule_id, requirement_id, expected
):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        response = _delete(client, token, schedule_id, requirement_id)
    assert response.status_code == expected
    if expected == 409:
        assert (
            response.json()["type"]
            == "urn:dreamplanner:problem:schedule-state-conflict"
        )
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("schedule_id", "requirement_id", "expected"),
    [(999999, LARGE_ID, 404), (SCHEDULE_ID, 999999, 404), (SCHEDULE_ID, 127, 404)],
)
def test_delete_scopes_requirement_to_existing_schedule(
    shift_requirements_api, schedule_id, requirement_id, expected
):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        response = _delete(client, token, schedule_id, requirement_id)
    assert response.status_code == expected
    assert response.headers["content-type"] == "application/problem+json"


def test_status_conflict_precedes_missing_requirement(shift_requirements_api):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        response = _delete(client, token, 46, 999999)
    assert response.status_code == 409
    assert response.json()["type"] == "urn:dreamplanner:problem:schedule-state-conflict"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/schedules/nope/shift-requirements/1",
        "/api/v1/schedules/45/shift-requirements/-1",
        "/api/v1/schedules/45/shift-requirements/9223372036854775808",
    ],
)
def test_invalid_path_ids_return_validation_problem(shift_requirements_api, path):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        response = client.delete(path, headers=_headers(token))
    assert response.status_code == 422
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert response.headers["cache-control"] == "no-store"


def test_delete_rejects_nonempty_request_body(shift_requirements_api):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        response = client.request(
            "DELETE",
            _detail_path(SCHEDULE_ID, LARGE_ID),
            headers=_headers(token),
            json={},
        )
    assert response.status_code == 422
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"


@pytest.mark.parametrize("assignment_count", [1, 3])
def test_assignments_prevent_delete_without_cascade(
    shift_requirements_api, assignment_count
):
    app, token, actor, engine = shift_requirements_api
    actor.role.code = "manager"
    _add_assignments(engine, count=assignment_count)
    with TestClient(app) as client:
        response = _delete(client, token)
    assert response.status_code == 409
    assert (
        response.json()["type"] == "urn:dreamplanner:problem:shift-requirement-assigned"
    )
    assert response.headers["cache-control"] == "no-store"
    with Session(engine) as session:
        assert session.get(ShiftRequirement, LARGE_ID) is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(ShiftAssignment)
                .where(ShiftAssignment.shift_requirement_id == LARGE_ID)
            )
            == assignment_count
        )


def test_rollback_when_flush_fails_preserves_requirement(shift_requirements_api):
    app, token, actor, engine = shift_requirements_api
    actor.role.code = "manager"

    def fail_requirement_delete(session, flush_context, instances):
        if any(isinstance(row, ShiftRequirement) for row in session.deleted):
            raise RuntimeError("simulated flush failure")

    event.listen(Session, "before_flush", fail_requirement_delete)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = _delete(client, token)
    finally:
        event.remove(Session, "before_flush", fail_requirement_delete)
    assert response.status_code == 500
    with Session(engine) as session:
        assert session.get(ShiftRequirement, LARGE_ID) is not None


def test_delete_openapi_documents_no_content_and_problem_responses(
    shift_requirements_api,
):
    app, _, _, _ = shift_requirements_api
    operation = app.openapi()["paths"][
        "/api/v1/schedules/{schedule_id}/shift-requirements/{requirement_id}"
    ]["delete"]
    assert operation["security"] == [{"BearerAuth": []}]
    assert {"204", "401", "403", "404", "409", "422"} <= set(operation["responses"])
    assert "content" not in operation["responses"]["204"]
    assert operation["responses"]["409"]["content"]["application/problem+json"]
