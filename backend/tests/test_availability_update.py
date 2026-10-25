"""HTTP and transaction checks for availability submission PATCH."""

from datetime import UTC, date, datetime, time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_availability_create import PATH, _headers

from src.availability.models import AvailabilityEntry, AvailabilitySubmission
from src.availability.repository import AvailabilitySubmissionRepository
from src.availability.service import _entry_conflict
from src.schedules.models import Schedule

pytest_plugins = ["test_availability_create"]


def _patch_path(submission_id: int) -> str:
    return f"{PATH}/{submission_id}"


def _seed_submission(engine, *, submitted_at=None, entries=None):
    with Session(engine) as session:
        submission = AvailabilitySubmission(
            schedule_id=45,
            user_id=42,
            submitted_at=submitted_at,
            created_at=datetime(2026, 10, 24, 10, tzinfo=UTC),
            updated_at=datetime(2026, 10, 24, 10, tzinfo=UTC),
        )
        session.add(submission)
        session.flush()
        for item in entries or []:
            session.add(
                AvailabilityEntry(
                    submission_id=submission.id,
                    work_date=item[0],
                    start_time=item[1],
                    end_time=item[2],
                    status=item[3],
                    note=item[4],
                )
            )
        session.commit()
        return submission.id


def _entry(day, start, end, status="available", note=None):
    return (
        date.fromisoformat(day),
        time.fromisoformat(start),
        time.fromisoformat(end),
        status,
        note,
    )


def test_patch_replaces_all_entries_and_updates_only_mutable_fields(create_api):
    app, token, _, engine = create_api
    submission_id = _seed_submission(
        engine,
        submitted_at=datetime(2026, 10, 23, 10, tzinfo=UTC),
        entries=[
            _entry("2027-01-05", "08:00:00", "10:00:00"),
            _entry("2027-01-06", "08:00:00", "10:00:00"),
        ],
    )
    with Session(engine) as session:
        other_submission = AvailabilitySubmission(
            schedule_id=45,
            user_id=77,
            created_at=datetime(2026, 10, 24, 10, tzinfo=UTC),
            updated_at=datetime(2026, 10, 24, 10, tzinfo=UTC),
        )
        session.add(other_submission)
        session.flush()
        session.add(
            AvailabilityEntry(
                id=500,
                submission_id=other_submission.id,
                work_date=date(2027, 1, 5),
                start_time=time(8),
                end_time=time(10),
                status="available",
            )
        )
        old_ids = {
            entry.id
            for entry in session.query(AvailabilityEntry).filter_by(
                submission_id=submission_id
            )
        }
        session.commit()
        old_created = session.get(AvailabilitySubmission, submission_id).created_at
        old_updated = session.get(AvailabilitySubmission, submission_id).updated_at
    with TestClient(app) as client:
        response = client.patch(
            _patch_path(submission_id),
            headers=_headers(token),
            json={
                "entries": [
                    {
                        "work_date": "2027-01-05",
                        "start_time": "10:00:00",
                        "end_time": "12:00:00",
                        "status": "unavailable",
                    }
                ]
            },
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["id"] == str(submission_id)
    assert body["submitted_at"] is None
    assert body["created_at"] == old_created.replace(tzinfo=UTC).isoformat().replace(
        "+00:00", "Z"
    )
    assert body["updated_at"] != old_updated.replace(tzinfo=UTC).isoformat().replace(
        "+00:00", "Z"
    )
    assert body["entries"][0]["status"] == "unavailable"
    assert {int(entry["id"]) for entry in body["entries"]}.isdisjoint(old_ids)
    with Session(engine) as session:
        assert (
            session.query(AvailabilityEntry)
            .filter_by(submission_id=submission_id)
            .count()
            == 1
        )


def test_patch_empty_entries_removes_all_and_downgrades_submission(create_api):
    app, token, _, engine = create_api
    submission_id = _seed_submission(
        engine,
        submitted_at=datetime(2026, 10, 23, 10, tzinfo=UTC),
        entries=[_entry("2027-01-05", "08:00:00", "10:00:00")],
    )
    with TestClient(app) as client:
        response = client.patch(
            _patch_path(submission_id), headers=_headers(token), json={"entries": []}
        )
    assert response.status_code == 200
    assert response.json()["entries"] == []
    assert response.json()["submitted_at"] is None


def test_submit_only_preserves_entries_and_sets_new_timestamp(create_api):
    app, token, _, engine = create_api
    submission_id = _seed_submission(
        engine,
        entries=[_entry("2027-01-05", "08:00:00", "10:00:00")],
    )
    with TestClient(app) as client:
        response = client.patch(
            _patch_path(submission_id), headers=_headers(token), json={"submit": True}
        )
    assert response.status_code == 200
    assert response.json()["submitted_at"] is not None
    assert response.json()["entries"][0]["start_time"] == "08:00:00"


def test_touching_intervals_are_valid_and_notes_are_normalized(create_api):
    app, token, _, engine = create_api
    submission_id = _seed_submission(engine)
    with TestClient(app) as client:
        response = client.patch(
            _patch_path(submission_id),
            headers=_headers(token),
            json={
                "entries": [
                    {
                        "work_date": "2027-01-05",
                        "start_time": "08:00:00",
                        "end_time": "10:00:00",
                        "status": "available",
                        "note": "  morning  ",
                    },
                    {
                        "work_date": "2027-01-05",
                        "start_time": "10:00:00",
                        "end_time": "12:00:00",
                        "status": "unavailable",
                        "note": "   ",
                    },
                ]
            },
        )
    assert response.status_code == 200
    assert [entry["status"] for entry in response.json()["entries"]] == [
        "available",
        "unavailable",
    ]
    assert [entry["note"] for entry in response.json()["entries"]] == [
        "morning",
        None,
    ]


@pytest.mark.parametrize(
    ("entries", "schedule_month"),
    [
        (
            [
                {
                    "work_date": "2027-02-05",
                    "start_time": "08:00:00",
                    "end_time": "09:00:00",
                    "status": "available",
                }
            ],
            1,
        ),
        (
            [
                {
                    "work_date": "2027-03-28",
                    "start_time": "01:30:00",
                    "end_time": "03:30:00",
                    "status": "available",
                }
            ],
            3,
        ),
        (
            [
                {
                    "work_date": "2027-10-31",
                    "start_time": "01:30:00",
                    "end_time": "03:30:00",
                    "status": "available",
                }
            ],
            10,
        ),
    ],
)
def test_invalid_month_or_dst_entry_is_422(create_api, entries, schedule_month):
    app, token, _, engine = create_api
    submission_id = _seed_submission(engine)
    with Session(engine) as session:
        session.get(Schedule, 45).month = schedule_month
        session.commit()
    with TestClient(app) as client:
        response = client.patch(
            _patch_path(submission_id),
            headers=_headers(token),
            json={"entries": entries},
        )
    assert response.status_code == 422


def test_inactive_account_is_rejected_by_current_authentication(create_api):
    app, token, actor, engine = create_api
    actor.is_active = False
    submission_id = _seed_submission(engine)
    with TestClient(app) as client:
        response = client.patch(
            _patch_path(submission_id),
            headers=_headers(token),
            json={"submit": True},
        )
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("submit", "expected_submitted"),
    [(None, False), (False, False), (True, True)],
)
def test_patch_entries_submit_variants(create_api, submit, expected_submitted):
    app, token, _, engine = create_api
    submission_id = _seed_submission(
        engine,
        submitted_at=datetime(2026, 10, 23, 10, tzinfo=UTC),
        entries=[_entry("2027-01-05", "08:00:00", "10:00:00")],
    )
    body = {
        "entries": [
            {
                "work_date": "2027-01-05",
                "start_time": "12:00:00",
                "end_time": "13:00:00",
                "status": "available",
            }
        ]
    }
    if submit is not None:
        body["submit"] = submit
    with TestClient(app) as client:
        response = client.patch(
            _patch_path(submission_id), headers=_headers(token), json=body
        )
    assert response.status_code == 200
    assert (response.json()["submitted_at"] is not None) is expected_submitted


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"submit": False},
        {"entries": None},
        {"submit": None},
        {"entries": [], "other": 1},
        {"entries": [], "id": "15"},
        {
            "entries": [
                {
                    "work_date": "2027-01-05",
                    "start_time": "08:00:00",
                    "end_time": "09:00:00",
                    "status": "available",
                    "id": "25",
                }
            ]
        },
        {
            "entries": [
                {
                    "work_date": "2027-01-05",
                    "start_time": "08:00:00",
                    "end_time": "09:00:00",
                    "status": "available",
                },
                {
                    "work_date": "2027-01-05",
                    "start_time": "08:30:00",
                    "end_time": "09:30:00",
                    "status": "unavailable",
                },
            ]
        },
    ],
)
def test_invalid_patch_is_problem_422(create_api, body):
    app, token, _, engine = create_api
    submission_id = _seed_submission(engine)
    with TestClient(app) as client:
        response = client.patch(
            _patch_path(submission_id), headers=_headers(token), json=body
        )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_non_employee_cannot_patch(create_api, role):
    app, token, actor, engine = create_api
    actor.role.code = role
    submission_id = _seed_submission(engine)
    with TestClient(app) as client:
        response = client.patch(
            _patch_path(submission_id), headers=_headers(token), json={"submit": True}
        )
    assert response.status_code == 403


def test_employee_cannot_patch_another_users_submission(create_api):
    app, token, _, engine = create_api
    submission_id = _seed_submission(engine)
    with Session(engine) as session:
        submission = session.get(AvailabilitySubmission, submission_id)
        submission.user_id = 77
        session.commit()
    with TestClient(app) as client:
        response = client.patch(
            _patch_path(submission_id), headers=_headers(token), json={"submit": True}
        )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "status", ["draft", "collection_closed", "closed", "published"]
)
def test_patch_requires_collecting_schedule(create_api, status):
    app, token, _, engine = create_api
    submission_id = _seed_submission(engine)
    with Session(engine) as session:
        session.get(Schedule, 45).status = status
        session.commit()
    with TestClient(app) as client:
        response = client.patch(
            _patch_path(submission_id), headers=_headers(token), json={"submit": True}
        )
    assert response.status_code == 409
    assert response.json()["type"] == "urn:dreamplanner:problem:schedule-state-conflict"


def test_patch_deletes_old_rows_before_inserting_and_commits_once(create_api):
    app, token, _, engine = create_api
    submission_id = _seed_submission(
        engine, entries=[_entry("2027-01-05", "08:00:00", "10:00:00")]
    )
    statements = []
    commits = []

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        normalized = statement.lstrip().upper()
        if normalized.startswith(("DELETE", "INSERT")):
            statements.append(normalized.split()[0])
        elif normalized.startswith("SELECT"):
            if "FROM SCHEDULES" in normalized:
                statements.append("LOCK_SCHEDULE")
            elif "FROM AVAILABILITY_SUBMISSIONS" in normalized:
                statements.append("LOCK_SUBMISSION")

    def record_commit(session):
        commits.append(True)

    event.listen(engine, "before_cursor_execute", record_sql)
    event.listen(Session, "before_commit", record_commit)
    try:
        with TestClient(app) as client:
            response = client.patch(
                _patch_path(submission_id),
                headers=_headers(token),
                json={
                    "entries": [
                        {
                            "work_date": "2027-01-05",
                            "start_time": "10:00:00",
                            "end_time": "12:00:00",
                            "status": "available",
                        }
                    ]
                },
            )
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
        event.remove(Session, "before_commit", record_commit)
    assert response.status_code == 200
    assert statements.index("LOCK_SCHEDULE") < statements.index("LOCK_SUBMISSION")
    delete_index = statements.index("DELETE")
    insert_index = statements.index("INSERT", delete_index + 1)
    assert delete_index < insert_index
    assert len(commits) == 1


def test_failed_entry_insert_rolls_back_old_entries_and_submission(
    create_api, monkeypatch
):
    app, token, _, engine = create_api
    old_timestamp = datetime(2026, 10, 23, 10, tzinfo=UTC)
    submission_id = _seed_submission(
        engine,
        submitted_at=old_timestamp,
        entries=[_entry("2027-01-05", "08:00:00", "10:00:00")],
    )

    class PgError(Exception):
        sqlstate = "23505"

        def __init__(self):
            self.diag = SimpleNamespace(constraint_name="uq_other_constraint")

    def fail_add(self, entries):
        raise IntegrityError("INSERT", {}, PgError())

    monkeypatch.setattr(AvailabilitySubmissionRepository, "add_entries", fail_add)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.patch(
            _patch_path(submission_id),
            headers=_headers(token),
            json={
                "entries": [
                    {
                        "work_date": "2027-01-05",
                        "start_time": "10:00:00",
                        "end_time": "12:00:00",
                        "status": "available",
                    }
                ],
                "submit": True,
            },
        )
    assert response.status_code == 500
    with Session(engine) as session:
        submission = session.get(AvailabilitySubmission, submission_id)
        assert submission.submitted_at == old_timestamp.replace(tzinfo=None)
        entries = (
            session.query(AvailabilityEntry)
            .filter_by(submission_id=submission_id)
            .all()
        )
        assert [(entry.start_time, entry.end_time) for entry in entries] == [
            (time(8), time(10))
        ]


def test_missing_token_and_missing_submission(create_api):
    app, token, _, _ = create_api
    with TestClient(app) as client:
        unauthenticated = client.patch(_patch_path(999), json={"submit": True})
        absent = client.patch(
            _patch_path(999), headers=_headers(token), json={"submit": True}
        )
    assert unauthenticated.status_code == 401
    assert absent.status_code == 404


def test_missing_schedule_and_submission_from_another_schedule_are_404(create_api):
    app, token, _, engine = create_api
    submission_id = _seed_submission(engine)
    with Session(engine) as session:
        session.add(
            Schedule(
                id=46,
                year=2027,
                month=2,
                status="collecting",
                created_by=100,
                created_at=datetime(2026, 10, 24, 10, tzinfo=UTC),
            )
        )
        session.commit()
    with TestClient(app) as client:
        missing_schedule = client.patch(
            f"/api/v1/schedules/999/availability-submissions/{submission_id}",
            headers=_headers(token),
            json={"submit": True},
        )
        other_schedule = client.patch(
            f"/api/v1/schedules/46/availability-submissions/{submission_id}",
            headers=_headers(token),
            json={"submit": True},
        )
    assert missing_schedule.status_code == 404
    assert other_schedule.status_code == 404


def test_known_entry_conflicts_are_matched_without_masking_other_integrity_errors():
    class PgError(Exception):
        def __init__(self, sqlstate, *, constraint=None, message=None):
            self.sqlstate = sqlstate
            self.diag = SimpleNamespace(
                constraint_name=constraint, message_primary=message
            )

    unique = IntegrityError(
        "INSERT", {}, PgError("23505", constraint="uq_availability_entry")
    )
    overlap = IntegrityError(
        "INSERT",
        {},
        PgError("P0001", message="Nakladajace sie przedzialy dyspozycyjnosci"),
    )
    other = IntegrityError(
        "INSERT", {}, PgError("23505", constraint="uq_other_constraint")
    )
    assert _entry_conflict(unique)
    assert _entry_conflict(overlap)
    assert not _entry_conflict(other)
