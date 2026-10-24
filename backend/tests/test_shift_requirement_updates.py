"""HTTP and transaction tests for partial shift requirement updates."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_shift_requirement_reads import (
    SCHEDULE_ID,
    _detail_path,
    _headers,
)
from test_shift_requirement_reads import (
    shift_requirements_api as shared_shift_requirements_api,
)

from src.assignments.models import ShiftAssignment
from src.schedules.models import ShiftRequirement
from src.schedules.service import _requirement_conflict

shift_requirements_api = shared_shift_requirements_api
REQUIREMENT_ID = 9007199254740993


def _patch(
    client, token, changes, *, schedule_id=SCHEDULE_ID, requirement_id=REQUIREMENT_ID
):
    return client.patch(
        _detail_path(schedule_id, requirement_id),
        headers=_headers(token),
        json=changes,
    )


def _assignment(engine, requirement_id=REQUIREMENT_ID):
    with Session(engine) as session:
        session.add(
            ShiftAssignment(
                id=1,
                shift_requirement_id=requirement_id,
                user_id=1,
                assigned_by=2,
            )
        )
        session.commit()


def test_patch_changes_fields_and_returns_complete_persisted_resource(
    shift_requirements_api,
):
    app, token, actor, engine = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        changed = _patch(
            client,
            token,
            {
                "position_id": "9007199254740997",
                "work_date": "2026-11-03",
                "start_time": "09:00:00",
                "end_time": "17:00:00",
                "required_workers": 4,
                "note": "  revised  ",
            },
        )
        partial = _patch(client, token, {"required_workers": 5})
    assert changed.status_code == 200
    assert changed.headers["cache-control"] == "no-store"
    assert changed.json() == {
        "id": str(REQUIREMENT_ID),
        "schedule_id": str(SCHEDULE_ID),
        "position_id": "9007199254740997",
        "position_name": "Duże ID",
        "position_is_active": True,
        "work_date": "2026-11-03",
        "start_time": "09:00:00",
        "end_time": "17:00:00",
        "required_workers": 4,
        "note": "revised",
    }
    assert partial.status_code == 200
    assert partial.json()["required_workers"] == 5
    with Session(engine) as session:
        requirement = session.get(ShiftRequirement, REQUIREMENT_ID)
        assert requirement is not None
        assert requirement.position_id == 9007199254740997
        assert requirement.required_workers == 5
        assert requirement.note == "revised"


def test_patch_note_null_blank_and_noop_structural_values(shift_requirements_api):
    app, token, actor, engine = shift_requirements_api
    actor.role.code = "manager"
    _assignment(engine)
    with TestClient(app) as client:
        unchanged = _patch(
            client,
            token,
            {
                "position_id": "7",
                "work_date": "2026-11-02",
                "start_time": "08:00:00",
                "end_time": "16:00:00",
                "required_workers": 2,
                "note": "memo",
            },
        )
        cleared = _patch(client, token, {"note": None})
        blank = _patch(client, token, {"note": " \t "})
    assert unchanged.status_code == cleared.status_code == blank.status_code == 200
    assert unchanged.json()["note"] == "memo"
    assert cleared.json()["note"] is None
    assert blank.json()["note"] is None
    with Session(engine) as session:
        assert session.get(ShiftRequirement, REQUIREMENT_ID).note is None
        assert session.get(ShiftAssignment, 1) is not None


@pytest.mark.parametrize(
    "role",
    ["admin", "employee"],
)
def test_non_manager_cannot_patch(shift_requirements_api, role):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = role
    with TestClient(app) as client:
        response = _patch(client, token, {"note": "x"})
    assert response.status_code == 403
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"


def test_missing_token_and_role_precedes_resource_lookup(shift_requirements_api):
    app, _, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        unauthenticated = client.patch(_detail_path(999999, 999999), json={"note": "x"})
        actor.role.code = "admin"
        forbidden = client.patch(
            _detail_path(999999, 999999),
            headers=_headers("invalid but unused"),
            json={"note": "x"},
        )
    assert unauthenticated.status_code == 401
    assert forbidden.status_code == 401


def test_admin_token_is_forbidden_before_resource_lookup(shift_requirements_api):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "admin"
    with TestClient(app) as client:
        response = client.patch(
            _detail_path(999999, 999999), headers=_headers(token), json={"note": "x"}
        )
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("schedule_id", "requirement_id", "expected"),
    [
        (45, REQUIREMENT_ID, 200),
        (46, 201, 409),
        (47, 202, 409),
        (48, 203, 409),
        (49, 204, 409),
    ],
)
def test_patch_respects_schedule_status(
    shift_requirements_api, schedule_id, requirement_id, expected
):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        response = _patch(
            client,
            token,
            {"note": "status check"},
            schedule_id=schedule_id,
            requirement_id=requirement_id,
        )
    assert response.status_code == expected
    if expected == 409:
        assert (
            response.json()["type"]
            == "urn:dreamplanner:problem:schedule-state-conflict"
        )


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"id": "5"},
        {"schedule_id": "45"},
        {"unknown": "x"},
        {"position_id": None},
        {"work_date": None},
        {"start_time": None},
        {"end_time": None},
        {"required_workers": None},
        {"position_id": "0"},
        {"position_id": "9223372036854775808"},
        {"position_id": 7},
        {"work_date": "2026-11-2"},
        {"work_date": "2026-02-30"},
        {"start_time": "08:00"},
        {"end_time": "16:00:00+01:00"},
        {"required_workers": 0},
        {"required_workers": -1},
        {"required_workers": 2147483648},
        {"required_workers": True},
        {"required_workers": "3"},
        {"work_date": "2026-10-20"},
        {"start_time": "16:00:00"},
        {"start_time": "20:00:00", "end_time": "08:00:00"},
    ],
)
def test_invalid_patch_returns_validation_problem(shift_requirements_api, changes):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        response = _patch(client, token, changes)
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"


@pytest.mark.parametrize(
    ("schedule_id", "requirement_id", "work_date", "start_time", "end_time"),
    [
        (51, 206, "2028-03-26", "02:30:00", "03:30:00"),
        (52, 207, "2028-10-29", "02:30:00", "03:30:00"),
    ],
)
def test_patch_rejects_dst_gap_and_fold(
    shift_requirements_api,
    schedule_id,
    requirement_id,
    work_date,
    start_time,
    end_time,
):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        response = _patch(
            client,
            token,
            {"work_date": work_date, "start_time": start_time, "end_time": end_time},
            schedule_id=schedule_id,
            requirement_id=requirement_id,
        )
    assert response.status_code == 422


def test_changed_position_must_exist_and_be_active_but_history_can_remain_inactive(
    shift_requirements_api,
):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        active = _patch(client, token, {"position_id": "9007199254740997"})
        inactive = _patch(client, token, {"position_id": "8"})
        missing = _patch(client, token, {"position_id": "999"})
        historical = _patch(
            client,
            token,
            {"note": "history retained"},
            requirement_id=124,
        )
    assert active.status_code == 200
    assert inactive.status_code == 409
    assert inactive.json()["type"] == "urn:dreamplanner:problem:position-inactive"
    assert missing.status_code == 404
    assert historical.status_code == 200
    assert historical.json()["position_is_active"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"position_id": "9007199254740997"},
        {"work_date": "2026-11-03"},
        {"start_time": "09:00:00"},
        {"end_time": "17:00:00"},
        {"required_workers": 3},
    ],
)
def test_assigned_requirement_rejects_each_real_structural_change(
    shift_requirements_api, changes
):
    app, token, actor, engine = shift_requirements_api
    actor.role.code = "manager"
    _assignment(engine)
    with TestClient(app) as client:
        response = _patch(client, token, changes)
    assert response.status_code == 409
    assert (
        response.json()["type"] == "urn:dreamplanner:problem:shift-requirement-assigned"
    )
    with Session(engine) as session:
        requirement = session.get(ShiftRequirement, REQUIREMENT_ID)
        assert requirement.position_id == 7
        assert requirement.work_date.isoformat() == "2026-11-02"
        assert requirement.start_time.isoformat() == "08:00:00"
        assert requirement.end_time.isoformat() == "16:00:00"
        assert requirement.required_workers == 2
        assert session.get(ShiftAssignment, 1) is not None


def test_missing_requirement_and_wrong_schedule_are_hidden(shift_requirements_api):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        missing = _patch(client, token, {"note": "x"}, requirement_id=999999)
        wrong_schedule = _patch(client, token, {"note": "x"}, requirement_id=127)
    assert missing.status_code == wrong_schedule.status_code == 404


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
        response = client.patch(path, headers=_headers(token), json={"note": "x"})
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"


def test_patch_keeps_overlapping_intervals_allowed(shift_requirements_api):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "manager"
    with TestClient(app) as client:
        response = _patch(client, token, {"start_time": "07:30:00"})
    assert response.status_code == 200
    assert response.json()["start_time"] == "07:30:00"


def test_duplicate_error_classifier_only_accepts_named_postgres_unique_violation():
    class PgUniqueError(Exception):
        sqlstate = "23505"

        def __init__(self, constraint_name):
            self.diag = type("Diagnostic", (), {"constraint_name": constraint_name})()

    exact = IntegrityError("UPDATE", {}, PgUniqueError("uq_shift_requirement"))
    unrelated = IntegrityError("UPDATE", {}, PgUniqueError("other_constraint"))
    assert _requirement_conflict(exact)
    assert not _requirement_conflict(unrelated)


@pytest.mark.parametrize(
    ("sqlstate", "constraint_name", "expected_status"),
    [
        ("23505", "uq_shift_requirement", 409),
        ("23514", "chk_shift_same_day", 500),
    ],
)
def test_patch_maps_only_the_named_unique_violation(
    shift_requirements_api,
    sqlstate,
    constraint_name,
    expected_status,
):
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "manager"

    class DatabaseError(Exception):
        def __init__(self):
            self.sqlstate = sqlstate
            self.diag = type("Diagnostic", (), {"constraint_name": constraint_name})()

    def fail_changed_requirement(session, flush_context, instances):
        if any(isinstance(obj, ShiftRequirement) for obj in session.dirty):
            raise IntegrityError("UPDATE", {}, DatabaseError())

    event.listen(Session, "before_flush", fail_changed_requirement)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = _patch(
                client,
                token,
                {"required_workers": 3},
            )
    finally:
        event.remove(Session, "before_flush", fail_changed_requirement)
    assert response.status_code == expected_status
    if expected_status == 409:
        assert (
            response.json()["type"]
            == "urn:dreamplanner:problem:shift-requirement-conflict"
        )
    else:
        assert response.headers["content-type"] == "application/problem+json"
        assert "chk_shift_same_day" not in response.text


def test_patch_openapi_documents_input_success_conflicts_and_auth(
    shift_requirements_api,
):
    app, _, _, _ = shift_requirements_api
    operation = app.openapi()["paths"][
        "/api/v1/schedules/{schedule_id}/shift-requirements/{requirement_id}"
    ]["patch"]
    assert operation["security"] == [{"BearerAuth": []}]
    assert {"200", "400", "401", "403", "404", "409", "422"} <= set(
        operation["responses"]
    )
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ShiftRequirementResponse"
    }
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/UpdateShiftRequirementInput"
    }
