"""HTTP checks for creating staffing requirements."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
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
from src.schedules.service import _requirement_conflict

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


def test_note_is_trimmed_and_null_is_preserved(shift_requirements_api):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        trimmed = client.post(
            _list_path(),
            headers=_headers(token),
            json=_payload(start_time="09:00:00", note="  opening  "),
        )
        explicit_null = client.post(
            _list_path(),
            headers=_headers(token),
            json=_payload(start_time="10:00:00", note=None),
        )
    assert trimmed.status_code == 201
    assert trimmed.json()["note"] == "opening"
    assert explicit_null.status_code == 201
    assert explicit_null.json()["note"] is None


def test_required_workers_accepts_postgres_integer_maximum(shift_requirements_api):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        response = client.post(
            _list_path(),
            headers=_headers(token),
            json=_payload(required_workers=2147483647),
        )
    assert response.status_code == 201
    assert response.json()["required_workers"] == 2147483647


def test_touching_and_overlapping_intervals_are_allowed(shift_requirements_api):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        first = client.post(_list_path(), headers=_headers(token), json=_payload())
        overlap = client.post(
            _list_path(),
            headers=_headers(token),
            json=_payload(start_time="12:00:00", end_time="20:00:00"),
        )
        touching = client.post(
            _list_path(),
            headers=_headers(token),
            json=_payload(start_time="16:00:00", end_time="18:00:00"),
        )
    assert [first.status_code, overlap.status_code, touching.status_code] == [
        201,
        201,
        201,
    ]


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


def test_role_is_checked_before_schedule_lookup(shift_requirements_api):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "admin"
    with TestClient(app) as client:
        response = client.post(
            _list_path(999999), headers=_headers(token), json=_payload()
        )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "overrides",
    [
        {"position_id": 7},
        {"position_id": "0"},
        {"work_date": "2026-11-2"},
        {"start_time": "08:00"},
        {"required_workers": True},
        {"required_workers": "2"},
        {"required_workers": 2147483648},
        {"note": "memo\n"},
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


@pytest.mark.parametrize(
    ("schedule_id", "work_date", "expected_status"),
    [
        (45, "2026-11-19", 201),
        (46, "2026-12-20", 409),
        (47, "2027-01-20", 409),
        (48, "2027-02-20", 409),
        (49, "2027-03-20", 409),
    ],
)
def test_each_schedule_status_is_enforced(
    shift_requirements_api, schedule_id, work_date, expected_status
):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        response = client.post(
            _list_path(schedule_id),
            headers=_headers(token),
            json=_payload(work_date=work_date),
        )
    assert response.status_code == expected_status


@pytest.mark.parametrize(
    ("schedule_id", "work_date", "start_time", "end_time"),
    [
        (51, "2028-03-26", "02:30:00", "03:30:00"),
        (52, "2028-10-29", "02:30:00", "03:30:00"),
    ],
)
def test_dst_gap_and_fold_intervals_are_rejected(
    shift_requirements_api, schedule_id, work_date, start_time, end_time
):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        response = client.post(
            _list_path(schedule_id),
            headers=_headers(token),
            json=_payload(
                work_date=work_date, start_time=start_time, end_time=end_time
            ),
        )
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"


def test_invalid_business_data_rolls_back_without_partial_requirement(
    shift_requirements_api,
):
    app, token, actor, engine = shift_requirements_api
    actor.role.code = "manager"
    with Session(engine) as session:
        count_before = session.query(ShiftRequirement).count()
    with TestClient(app) as client:
        response = client.post(
            _list_path(),
            headers=_headers(token),
            json=_payload(work_date="2026-10-20"),
        )
    assert response.status_code == 422
    with Session(engine) as session:
        assert session.query(ShiftRequirement).count() == count_before


def test_duplicate_classifier_requires_unique_violation_and_named_constraint():
    class PgError(Exception):
        sqlstate = "23505"

        def __init__(self, constraint_name):
            self.diag = type("Diagnostic", (), {"constraint_name": constraint_name})()

    duplicate = IntegrityError("INSERT", {}, PgError("uq_shift_requirement"))
    other_unique = IntegrityError("INSERT", {}, PgError("some_other_unique"))
    other_sqlstate = IntegrityError(
        "INSERT", {}, type("ForeignKeyError", (), {"sqlstate": "23503"})()
    )
    assert _requirement_conflict(duplicate)
    assert not _requirement_conflict(other_unique)
    assert not _requirement_conflict(other_sqlstate)
