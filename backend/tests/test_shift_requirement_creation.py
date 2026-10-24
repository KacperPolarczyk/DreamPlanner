"""HTTP checks for creating staffing requirements."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_shift_requirement_reads import (
    SCHEDULE_ID,
    _detail_path,
    _headers,
    _list_path,
)
from test_shift_requirement_reads import (
    shift_requirements_api as shared_shift_requirements_api,
)

from src.schedules.models import ShiftRequirement

shift_requirements_api = shared_shift_requirements_api


def _payload(**overrides):
    payload = {
        "position_id": "7",
        "work_date": "2026-11-20",
        "start_time": "08:00:00",
        "end_time": "16:00:00",
        "required_workers": 2,
    }
    return payload | overrides


def test_manager_creates_and_reads_committed_requirement(shift_requirements_api):
    app, token, actor, engine = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        response = client.post(
            _list_path(), headers=_headers(token), json=_payload(note="  ")
        )
        assert response.status_code == 201
        requirement_id = response.json()["id"]
        assert response.headers["Location"] == _detail_path(
            SCHEDULE_ID, int(requirement_id)
        )
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json() == {
            "id": requirement_id,
            "schedule_id": str(SCHEDULE_ID),
            "position_id": "7",
            "position_name": "Kasjer",
            "position_is_active": True,
            "work_date": "2026-11-20",
            "start_time": "08:00:00",
            "end_time": "16:00:00",
            "required_workers": 2,
            "note": None,
        }
        assert (
            client.get(
                _detail_path(SCHEDULE_ID, int(requirement_id)), headers=_headers(token)
            ).json()
            == response.json()
        )
        assert any(
            item["id"] == requirement_id
            for item in client.get(_list_path(), headers=_headers(token)).json()[
                "items"
            ]
        )
    with Session(engine) as session:
        saved = session.get(ShiftRequirement, int(requirement_id))
        assert saved is not None and saved.note is None


@pytest.mark.parametrize("role", ["admin", "employee"])
def test_non_manager_cannot_create(shift_requirements_api, role):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = role
    with TestClient(app) as client:
        response = client.post(_list_path(), headers=_headers(token), json=_payload())
    assert response.status_code == 403


def test_unauthenticated_cannot_create(shift_requirements_api):
    app, _, _, _ = shift_requirements_api
    with TestClient(app) as client:
        assert client.post(_list_path(), json=_payload()).status_code == 401


@pytest.mark.parametrize(
    "overrides",
    [
        {"position_id": 7},
        {"position_id": "0"},
        {"work_date": "2026-11-2"},
        {"start_time": "08:00"},
        {"required_workers": True},
        {"required_workers": "2"},
        {"id": "4"},
        {"schedule_id": "45"},
        {"unexpected": "x"},
        {"work_date": "2026-10-20"},
        {"start_time": "16:00:00"},
    ],
)
def test_invalid_creation_payload_is_422(shift_requirements_api, overrides):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        response = client.post(
            _list_path(), headers=_headers(token), json=_payload(**overrides)
        )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


def test_inactive_and_missing_position(shift_requirements_api):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        inactive = client.post(
            _list_path(), headers=_headers(token), json=_payload(position_id="8")
        )
        missing = client.post(
            _list_path(), headers=_headers(token), json=_payload(position_id="999")
        )
    assert inactive.status_code == 409
    assert inactive.json()["type"] == "urn:dreamplanner:problem:position-inactive"
    assert missing.status_code == 404


def test_schedule_state_conflict_and_duplicate(shift_requirements_api):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        body = _payload(work_date="2026-12-20")
        first = client.post(_list_path(46), headers=_headers(token), json=body)
        assert first.status_code == 409
        assert (
            first.json()["type"] == "urn:dreamplanner:problem:schedule-state-conflict"
        )
