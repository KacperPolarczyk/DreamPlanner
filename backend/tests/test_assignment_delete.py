"""HTTP and transaction checks for deleting a schedule-scoped assignment."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from test_assignment_create import PATH, _body, _headers, _post

from src.assignments.models import ShiftAssignment
from src.assignments.repository import ShiftAssignmentRepository
from src.schedules.models import Schedule, ShiftRequirement
from src.schedules.repository import ScheduleRepository

pytest_plugins = ["test_assignment_create"]


def _path(assignment_id: int | str, schedule_id: int | str = 45) -> str:
    return f"{PATH.rsplit('/', 2)[0]}/{schedule_id}/shift-assignments/{assignment_id}"


def _seed(engine, *, requirement_id: int = 31, user_id: int = 8) -> int:
    with Session(engine) as session:
        assignment = ShiftAssignment(
            shift_requirement_id=requirement_id,
            user_id=user_id,
            assigned_by=7,
            assigned_at=datetime(2026, 10, 24, tzinfo=UTC),
        )
        session.add(assignment)
        session.commit()
        return assignment.id


def _ids(engine) -> list[int]:
    with Session(engine) as session:
        return list(
            session.scalars(select(ShiftAssignment.id).order_by(ShiftAssignment.id))
        )


def test_manager_delete_is_empty_and_visible_in_reads(assignment_api) -> None:
    app, token, _, engine = assignment_api
    removed_id = _seed(engine)
    remaining_id = _seed(engine, requirement_id=32, user_id=9)
    with TestClient(app) as client:
        before = client.get(PATH, headers=_headers(token))
        response = client.delete(_path(removed_id), headers=_headers(token))
        detail = client.get(_path(removed_id), headers=_headers(token))
        listed = client.get(PATH, headers=_headers(token))
        repeated = client.delete(_path(removed_id), headers=_headers(token))
    assert before.json()["total"] == 2
    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert detail.status_code == repeated.status_code == 404
    assert listed.json()["total"] == 1
    assert [item["id"] for item in listed.json()["items"]] == [str(remaining_id)]
    assert _ids(engine) == [remaining_id]
    with Session(engine) as session:
        assert session.get(ShiftRequirement, 31).required_workers == 3
        assert session.get(Schedule, 45).status == "closed"


def test_empty_zero_length_body_is_valid(assignment_api) -> None:
    app, token, _, engine = assignment_api
    assignment_id = _seed(engine)
    with TestClient(app) as client:
        response = client.request(
            "DELETE", _path(assignment_id), headers=_headers(token), content=b""
        )
    assert response.status_code == 204
    assert _ids(engine) == []


def test_delete_does_not_require_active_assignee_or_position(assignment_api) -> None:
    app, token, _, engine = assignment_api
    assignment_id = _seed(engine, user_id=10)
    with TestClient(app) as client:
        response = client.delete(_path(assignment_id), headers=_headers(token))
    assert response.status_code == 204
    assert _ids(engine) == []


def test_delete_frees_capacity_for_later_post(assignment_api) -> None:
    app, token, _, engine = assignment_api
    assignment_id = _seed(engine)
    with Session(engine) as session:
        session.get(ShiftRequirement, 31).required_workers = 1
        session.commit()
    with TestClient(app) as client:
        removed = client.delete(_path(assignment_id), headers=_headers(token))
    created = _post(app, token, _body(user_id="9", acknowledged=True))
    assert removed.status_code == 204
    assert created.status_code == 201
    assert created.json()["user_id"] == "9"
    assert len(_ids(engine)) == 1


@pytest.mark.parametrize("role", ["admin", "employee"])
def test_role_is_checked_before_resource_or_body(assignment_api, role) -> None:
    app, token, actor, engine = assignment_api
    assignment_id = _seed(engine)
    actor.role.code = role
    with TestClient(app) as client:
        response = client.request(
            "DELETE", _path(999, 999), headers=_headers(token), content=b"{}"
        )
    assert response.status_code == 403
    assert response.headers["content-type"] == "application/problem+json"
    assert _ids(engine) == [assignment_id]


def test_authentication_failures(assignment_api) -> None:
    app, token, actor, engine = assignment_api
    assignment_id = _seed(engine)
    with TestClient(app) as client:
        missing = client.delete(_path(assignment_id))
        invalid = client.delete(_path(assignment_id), headers=_headers("invalid"))
        actor.is_active = False
        inactive = client.delete(_path(assignment_id), headers=_headers(token))
    for response in (missing, invalid, inactive):
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-type"] == "application/problem+json"
    assert _ids(engine) == [assignment_id]


@pytest.mark.parametrize(
    "status", ["draft", "collecting", "collection_closed", "published"]
)
def test_only_closed_allows_delete(assignment_api, status) -> None:
    app, token, _, engine = assignment_api
    assignment_id = _seed(engine)
    with Session(engine) as session:
        session.get(Schedule, 45).status = status
        session.commit()
    with TestClient(app) as client:
        response = client.delete(_path(assignment_id), headers=_headers(token))
    assert response.status_code == 409
    assert response.json()["type"] == "urn:dreamplanner:problem:schedule-state-conflict"
    assert _ids(engine) == [assignment_id]


def test_scope_and_missing_resources(assignment_api) -> None:
    app, token, _, engine = assignment_api
    assignment_id = _seed(engine, requirement_id=35)
    with TestClient(app) as client:
        missing_schedule = client.delete(
            _path(assignment_id, 999), headers=_headers(token)
        )
        missing_assignment = client.delete(_path(999), headers=_headers(token))
        foreign = client.delete(_path(assignment_id), headers=_headers(token))
    assert [r.status_code for r in (missing_schedule, missing_assignment, foreign)] == [
        404
    ] * 3
    assert (
        len(
            {
                r.json()["detail"]
                for r in (missing_schedule, missing_assignment, foreign)
            }
        )
        == 1
    )
    assert _ids(engine) == [assignment_id]


@pytest.mark.parametrize("body", [b"{}", b"[]", b'{"id":1}'])
def test_nonempty_body_is_rejected_before_delete(assignment_api, body) -> None:
    app, token, _, engine = assignment_api
    assignment_id = _seed(engine)
    with TestClient(app) as client:
        response = client.request(
            "DELETE", _path(assignment_id), headers=_headers(token), content=body
        )
    assert response.status_code == 422
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert _ids(engine) == [assignment_id]


@pytest.mark.parametrize("identifier", ["-1", "+1", "１２", "9223372036854775808"])
def test_invalid_path_id_is_422_without_changes(assignment_api, identifier) -> None:
    app, token, _, engine = assignment_api
    assignment_id = _seed(engine)
    with TestClient(app) as client:
        response = client.delete(_path(identifier), headers=_headers(token))
    assert response.status_code == 422
    assert _ids(engine) == [assignment_id]


def test_lock_order_and_one_commit(assignment_api, monkeypatch) -> None:
    app, token, _, engine = assignment_api
    assignment_id = _seed(engine)
    calls = []
    for owner, name in (
        (ScheduleRepository, "get_by_id_for_update"),
        (ShiftAssignmentRepository, "get_for_schedule_for_update"),
        (ShiftAssignmentRepository, "delete"),
    ):
        original = getattr(owner, name)

        def wrapped(self, *args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(owner, name, wrapped)

    def committed(session):
        if session.bind is engine:
            calls.append("commit")

    event.listen(Session, "after_commit", committed)
    try:
        with TestClient(app) as client:
            response = client.delete(_path(assignment_id), headers=_headers(token))
    finally:
        event.remove(Session, "after_commit", committed)
    assert response.status_code == 204
    assert calls == [
        "get_by_id_for_update",
        "get_for_schedule_for_update",
        "delete",
        "commit",
    ]


def test_denied_delete_does_not_commit(assignment_api) -> None:
    app, token, _, engine = assignment_api
    assignment_id = _seed(engine)
    commits = []

    def committed(session):
        if session.bind is engine:
            commits.append(1)

    event.listen(Session, "after_commit", committed)
    try:
        with TestClient(app) as client:
            response = client.delete(_path(999), headers=_headers(token))
    finally:
        event.remove(Session, "after_commit", committed)
    assert response.status_code == 404
    assert commits == []
    assert _ids(engine) == [assignment_id]


def test_scoped_lock_sql_and_no_repository_commit() -> None:
    statements = []

    class RecordingSession:
        def scalar(self, statement):
            statements.append(statement)
            return None

    repository = ShiftAssignmentRepository(RecordingSession())
    repository.get_for_schedule_for_update(45, 101)
    sql = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "JOIN shift_requirements" in sql
    assert "shift_requirements.schedule_id" in sql
    assert "shift_assignments.id" in sql
    assert "FOR UPDATE OF shift_assignments" in sql


def test_error_after_stage_rolls_back_without_commit(
    assignment_api, monkeypatch
) -> None:
    app, token, _, engine = assignment_api
    assignment_id = _seed(engine)
    original_delete = ShiftAssignmentRepository.delete
    original_rollback = Session.rollback
    commits = []
    rollbacks = []

    def fail_after_stage(repository, assignment):
        original_delete(repository, assignment)
        raise RuntimeError("simulated persistence failure")

    monkeypatch.setattr(ShiftAssignmentRepository, "delete", fail_after_stage)
    monkeypatch.setattr(Session, "commit", lambda session: commits.append(session))
    monkeypatch.setattr(
        Session,
        "rollback",
        lambda session: (rollbacks.append(session), original_rollback(session))[1],
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.delete(_path(assignment_id), headers=_headers(token))
    assert response.status_code == 500
    assert commits == []
    assert rollbacks
    assert _ids(engine) == [assignment_id]


def test_openapi_documents_delete_without_patch(assignment_api) -> None:
    app, _, _, _ = assignment_api
    detail = "/api/v1/schedules/{schedule_id}/shift-assignments/{assignment_id}"
    operations = app.openapi()["paths"][detail]
    assert set(operations) == {"get", "delete"}
    operation = operations["delete"]
    assert operation["security"] == [{"BearerAuth": []}]
    assert "manager" in operation["description"]
    assert "Niepuste body" in operation["description"]
    assert "requestBody" not in operation
    assert {"204", "401", "403", "404", "409", "422"} <= set(operation["responses"])
    assert "content" not in operation["responses"]["204"]
    assert "application/problem+json" in operation["responses"]["409"]["content"]
