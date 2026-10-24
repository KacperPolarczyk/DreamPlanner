"""HTTP and transaction checks for availability submission DELETE."""

from datetime import UTC, date, datetime, time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session
from test_availability_create import NOW, PATH, _headers

from src.availability.models import AvailabilityEntry, AvailabilitySubmission
from src.availability.repository import AvailabilitySubmissionRepository
from src.schedules.models import Schedule

pytest_plugins = ["test_availability_create"]


def _delete_path(submission_id: int, schedule_id: int = 45) -> str:
    return f"/api/v1/schedules/{schedule_id}/availability-submissions/{submission_id}"


def _seed_submission(
    engine, *, submitted_at=None, schedule_id=45, user_id=42, entries=()
):
    with Session(engine) as session:
        submission = AvailabilitySubmission(
            schedule_id=schedule_id,
            user_id=user_id,
            submitted_at=submitted_at,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(submission)
        session.flush()
        for day, start, end in entries:
            session.add(
                AvailabilityEntry(
                    submission_id=submission.id,
                    work_date=date.fromisoformat(day),
                    start_time=time.fromisoformat(start),
                    end_time=time.fromisoformat(end),
                    status="available",
                )
            )
        session.commit()
        return submission.id


@pytest.mark.parametrize("entries", [(), (("2027-01-05", "08:00:00", "10:00:00"),)])
def test_delete_owned_draft_returns_empty_204_and_removes_entries(create_api, entries):
    app, token, _, engine = create_api
    submission_id = _seed_submission(engine, entries=entries)

    with TestClient(app) as client:
        response = client.delete(_delete_path(submission_id), headers=_headers(token))
        assert response.status_code == 204
        assert response.content == b""
        assert response.headers["cache-control"] == "no-store"
        repeated = client.delete(_delete_path(submission_id), headers=_headers(token))

    assert repeated.status_code == 404
    assert repeated.headers["content-type"].startswith("application/problem+json")
    assert repeated.headers["cache-control"] == "no-store"
    with Session(engine) as session:
        assert session.get(AvailabilitySubmission, submission_id) is None
        # This SQLite fixture disables FK enforcement. PostgreSQL's real
        # ON DELETE CASCADE is verified in the dedicated database step.
        assert (
            next(
                iter(AvailabilityEntry.__table__.c.submission_id.foreign_keys)
            ).ondelete
            == "CASCADE"
        )


def test_deleted_draft_is_absent_from_later_list_and_detail(create_api):
    app, token, _, engine = create_api
    submission_id = _seed_submission(engine)
    with TestClient(app) as client:
        client.delete(_delete_path(submission_id), headers=_headers(token))
        listed = client.get(PATH, headers=_headers(token))
        detailed = client.get(_delete_path(submission_id), headers=_headers(token))
    assert listed.status_code == 200
    assert listed.json()["items"] == []
    assert detailed.status_code == 404


@pytest.mark.parametrize("entries", [(), (("2027-01-05", "08:00:00", "10:00:00"),)])
def test_submitted_declaration_cannot_be_deleted(create_api, entries):
    app, token, _, engine = create_api
    submission_id = _seed_submission(
        engine, submitted_at=datetime(2026, 10, 24, tzinfo=UTC), entries=entries
    )
    with TestClient(app) as client:
        response = client.delete(_delete_path(submission_id), headers=_headers(token))
    assert response.status_code == 409
    assert response.json()["type"] == (
        "urn:dreamplanner:problem:availability-submission-submitted"
    )
    with Session(engine) as session:
        assert session.get(AvailabilitySubmission, submission_id) is not None
        assert session.query(AvailabilityEntry).filter_by(
            submission_id=submission_id
        ).count() == len(entries)


@pytest.mark.parametrize(
    "status", ["draft", "collection_closed", "closed", "published"]
)
def test_delete_requires_collecting_schedule(create_api, status):
    app, token, _, engine = create_api
    submission_id = _seed_submission(engine)
    with Session(engine) as session:
        session.get(Schedule, 45).status = status
        session.commit()
    with TestClient(app) as client:
        response = client.delete(_delete_path(submission_id), headers=_headers(token))
    assert response.status_code == 409
    assert response.json()["type"] == "urn:dreamplanner:problem:schedule-state-conflict"
    with Session(engine) as session:
        assert session.get(AvailabilitySubmission, submission_id) is not None


def test_owner_filter_and_resource_scope_return_same_not_found(create_api):
    app, token, _, engine = create_api
    foreign_id = _seed_submission(engine, user_id=77)
    with Session(engine) as session:
        session.add(
            Schedule(
                id=46,
                year=2027,
                month=2,
                status="collecting",
                created_by=100,
                created_at=NOW,
            )
        )
        session.commit()
    other_schedule_id = _seed_submission(engine, schedule_id=46)
    with TestClient(app) as client:
        foreign = client.delete(_delete_path(foreign_id), headers=_headers(token))
        other_schedule = client.delete(
            _delete_path(other_schedule_id), headers=_headers(token)
        )
        missing = client.delete(_delete_path(9999), headers=_headers(token))
    assert [r.status_code for r in (foreign, other_schedule, missing)] == [404] * 3
    assert len({r.json()["detail"] for r in (foreign, other_schedule, missing)}) == 1


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_manager_and_admin_cannot_delete(create_api, role):
    app, token, actor, _ = create_api
    actor.role.code = role
    with TestClient(app) as client:
        response = client.delete(_delete_path(9999), headers=_headers(token))
    assert response.status_code == 403


def test_missing_token_and_inactive_account_are_unauthorized(create_api):
    app, token, actor, _ = create_api
    with TestClient(app) as client:
        missing = client.delete(_delete_path(1))
        invalid = client.delete(_delete_path(1), headers=_headers("invalid"))
        actor.is_active = False
        inactive = client.delete(_delete_path(1), headers=_headers(token))
    for response in (missing, invalid, inactive):
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.headers["content-type"].startswith("application/problem+json")


def test_missing_schedule_is_not_found(create_api):
    app, token, _, _ = create_api
    with TestClient(app) as client:
        response = client.delete(
            _delete_path(1, schedule_id=9999), headers=_headers(token)
        )
    assert response.status_code == 404


def test_lock_queries_have_schedule_then_owned_submission_order(create_api):
    app, token, _, engine = create_api
    submission_id = _seed_submission(engine)
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def record_selects(connection, cursor, statement, parameters, context, executemany):
        normalized = statement.upper()
        if "SELECT" in normalized and (
            "FROM SCHEDULES" in normalized
            or "FROM AVAILABILITY_SUBMISSIONS" in normalized
        ):
            statements.append(normalized)

    with TestClient(app) as client:
        response = client.delete(_delete_path(submission_id), headers=_headers(token))
    assert response.status_code == 204
    schedule_index = next(i for i, sql in enumerate(statements) if "SCHEDULES" in sql)
    submission_index = next(
        i for i, sql in enumerate(statements) if "AVAILABILITY_SUBMISSIONS" in sql
    )
    assert schedule_index < submission_index
    assert "USER_ID" in statements[submission_index]


def test_delete_commits_exactly_once(create_api, monkeypatch):
    app, token, _, engine = create_api
    submission_id = _seed_submission(engine)
    original_commit = Session.commit
    commits = []

    def count_commit(session):
        commits.append(session)
        return original_commit(session)

    monkeypatch.setattr(Session, "commit", count_commit)
    with TestClient(app) as client:
        response = client.delete(_delete_path(submission_id), headers=_headers(token))
    assert response.status_code == 204
    assert len(commits) == 1


def test_error_after_staging_delete_rolls_back_without_commit(create_api, monkeypatch):
    app, token, _, engine = create_api
    submission_id = _seed_submission(engine)
    original_delete = AvailabilitySubmissionRepository.delete_submission
    original_rollback = Session.rollback
    commits = []
    rollbacks = []

    def fail_after_stage(repository, submission):
        original_delete(repository, submission)
        raise RuntimeError("simulated persistence failure")

    monkeypatch.setattr(
        AvailabilitySubmissionRepository, "delete_submission", fail_after_stage
    )
    monkeypatch.setattr(Session, "commit", lambda session: commits.append(session))
    monkeypatch.setattr(
        Session,
        "rollback",
        lambda session: (rollbacks.append(session), original_rollback(session))[1],
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.delete(_delete_path(submission_id), headers=_headers(token))
    assert response.status_code == 500
    assert commits == []
    assert rollbacks
    with Session(engine) as session:
        assert session.get(AvailabilitySubmission, submission_id) is not None


def test_delete_openapi_documents_employee_rbac_and_problem_responses(create_api):
    app, _, _, _ = create_api
    operation = app.openapi()["paths"][
        "/api/v1/schedules/{schedule_id}/availability-submissions/{submission_id}"
    ]["delete"]
    assert "employee" in operation["description"]
    assert {"204", "401", "403", "404", "409", "422"} <= set(operation["responses"])
    assert "content" not in operation["responses"]["204"]
    assert "application/problem+json" in operation["responses"]["409"]["content"]


@pytest.mark.parametrize("identifier", ["+1", "-1", "１２", "9223372036854775808"])
def test_invalid_path_ids_return_validation_problem(create_api, identifier):
    app, token, _, _ = create_api
    path = f"/api/v1/schedules/{identifier}/availability-submissions/1"
    with TestClient(app) as client:
        response = client.delete(path, headers=_headers(token))
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


def test_repository_delete_is_staged_without_commit():
    repository = object.__new__(AvailabilitySubmissionRepository)
    deleted = []
    repository.session = type("SessionStub", (), {"delete": deleted.append})()
    submission = object()
    repository.delete_submission(submission)
    assert deleted == [submission]
