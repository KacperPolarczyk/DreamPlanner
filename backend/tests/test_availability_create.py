"""Creation, authorization, and validation tests for availability submissions."""

import base64
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Integer, MetaData, create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.auth import session_service
from src.availability.models import AvailabilitySubmission
from src.availability.service import _submission_conflict
from src.core.database import Base, get_session
from src.main import create_app
from src.schedules.models import Schedule

PATH = "/api/v1/schedules/45/availability-submissions"
NOW = datetime(2026, 10, 24, 10, tzinfo=UTC)


@pytest.fixture
def create_api(monkeypatch: pytest.MonkeyPatch):
    """Provide an isolated SQLite database and current JWT-backed identity."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "availability-create-test-v1")
    monkeypatch.setenv("JWT_ISSUER", "dreamplanner-availability-create-test")
    monkeypatch.setenv("JWT_AUDIENCE", "dreamplanner-availability-create-api")
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def disable_sqlite_fk(connection, record) -> None:
        connection.execute("PRAGMA foreign_keys=OFF")

    test_metadata = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(test_metadata)
    for table_name in (
        "schedules",
        "availability_submissions",
        "availability_entries",
    ):
        test_metadata.tables[table_name].c.id.type = Integer()
    test_metadata.create_all(
        engine,
        tables=[
            test_metadata.tables["schedules"],
            test_metadata.tables["availability_submissions"],
            test_metadata.tables["availability_entries"],
        ],
    )
    with Session(engine) as session:
        session.add(
            Schedule(
                id=45,
                year=2027,
                month=1,
                status="collecting",
                created_by=100,
                created_at=NOW,
            )
        )
        session.commit()

    app = create_app()
    session_id = uuid4()
    actor = SimpleNamespace(
        id=42, is_active=True, activated_at=NOW, role=SimpleNamespace(code="employee")
    )
    auth_session = SimpleNamespace(
        user_id=42,
        revoked_at=None,
        idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
        absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
    )

    class AuthRepository:
        def get_session(self, requested_id):
            return auth_session if requested_id == session_id else None

        def get_user_fresh(self, requested_id):
            return actor if requested_id == 42 else None

    monkeypatch.setattr(
        session_service, "AuthSessionRepository", lambda session: AuthRepository()
    )

    def test_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    token = app.state.jwt_codec.issue(42, session_id)
    try:
        yield app, token, actor, engine
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_create_empty_draft_and_submission(create_api):
    app, token, _, engine = create_api
    with TestClient(app) as client:
        draft = client.post(PATH, headers=_headers(token), json={"entries": []})
    assert draft.status_code == 201
    assert draft.json()["submitted_at"] is None
    assert draft.headers["location"].endswith(f"/{draft.json()['id']}")
    assert draft.headers["cache-control"] == "no-store"
    with Session(engine) as session:
        session.query(AvailabilitySubmission).filter_by(
            id=int(draft.json()["id"])
        ).delete()
        session.commit()
    with TestClient(app) as client:
        submitted = client.post(
            PATH, headers=_headers(token), json={"entries": [], "submit": True}
        )
    assert submitted.status_code == 201
    assert submitted.json()["submitted_at"].endswith("Z")
    assert submitted.json()["entries"] == []
    assert submitted.json()["created_at"] == submitted.json()["updated_at"]


def test_create_sorts_entries_and_normalizes_notes(create_api):
    app, token, _, _ = create_api
    with TestClient(app) as client:
        response = client.post(
            PATH,
            headers=_headers(token),
            json={
                "entries": [
                    {
                        "work_date": "2027-01-06",
                        "start_time": "10:00:00",
                        "end_time": "12:00:00",
                        "status": "unavailable",
                    },
                    {
                        "work_date": "2027-01-05",
                        "start_time": "12:00:00",
                        "end_time": "13:00:00",
                        "status": "available",
                        "note": "  hello  ",
                    },
                    {
                        "work_date": "2027-01-05",
                        "start_time": "08:00:00",
                        "end_time": "10:00:00",
                        "status": "available",
                        "note": "  ",
                    },
                ]
            },
        )
    assert response.status_code == 201
    entries = response.json()["entries"]
    assert [entry["start_time"] for entry in entries] == [
        "08:00:00",
        "12:00:00",
        "10:00:00",
    ]
    assert entries[1]["note"] == "hello"
    assert entries[0]["note"] is None
    assert all(isinstance(entry["id"], str) for entry in entries)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"entries": None},
        {"entries": [], "submit": None},
        {"entries": [], "unexpected": 1},
        {
            "entries": [
                {
                    "work_date": "2027-1-05",
                    "start_time": "08:00:00",
                    "end_time": "09:00:00",
                    "status": "available",
                }
            ]
        },
        {
            "entries": [
                {
                    "work_date": "2027-01-05",
                    "start_time": "24:00:00",
                    "end_time": "25:00:00",
                    "status": "available",
                }
            ]
        },
        {
            "entries": [
                {
                    "work_date": "2027-01-05",
                    "start_time": "08:00:00",
                    "end_time": "09:00:00",
                    "status": "other",
                }
            ]
        },
        {
            "entries": [
                {
                    "work_date": "2027-01-05",
                    "start_time": "08:00:00",
                    "end_time": "10:00:00",
                    "status": "available",
                },
                {
                    "work_date": "2027-01-05",
                    "start_time": "09:00:00",
                    "end_time": "11:00:00",
                    "status": "unavailable",
                },
            ]
        },
        {
            "entries": [
                {
                    "work_date": "2027-01-05",
                    "start_time": "08:00:00",
                    "end_time": "10:00:00",
                    "status": "available",
                },
                {
                    "work_date": "2027-01-05",
                    "start_time": "08:00:00",
                    "end_time": "10:00:00",
                    "status": "available",
                },
            ]
        },
        {
            "entries": [
                {
                    "work_date": "2027-01-05",
                    "start_time": "08:00:00",
                    "end_time": "10:00:00",
                    "status": "available",
                    "note": "bad\u0001note",
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
                    "id": "10",
                }
            ]
        },
    ],
)
def test_invalid_request_is_422(create_api, body):
    app, token, _, _ = create_api
    with TestClient(app) as client:
        response = client.post(PATH, headers=_headers(token), json=body)
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize(
    "status", ["draft", "collection_closed", "closed", "published"]
)
def test_non_collecting_schedule_is_conflict(create_api, status):
    app, token, _, engine = create_api
    with Session(engine) as session:
        session.get(Schedule, 45).status = status
        session.commit()
    with TestClient(app) as client:
        response = client.post(PATH, headers=_headers(token), json={"entries": []})
    assert response.status_code == 409


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_non_employee_is_forbidden(create_api, role):
    app, token, actor, _ = create_api
    actor.role.code = role
    with TestClient(app) as client:
        response = client.post(PATH, headers=_headers(token), json={"entries": []})
    assert response.status_code == 403


def test_duplicate_submission_conflicts_without_mutation(create_api):
    app, token, _, engine = create_api
    with TestClient(app) as client:
        first = client.post(PATH, headers=_headers(token), json={"entries": []})
        second = client.post(
            PATH, headers=_headers(token), json={"entries": [], "submit": True}
        )
    assert first.status_code == 201
    assert second.status_code == 409
    with Session(engine) as session:
        assert session.query(AvailabilitySubmission).count() == 1


def test_only_named_postgres_unique_violation_is_a_submission_conflict():
    class PgError(Exception):
        sqlstate = "23505"

        def __init__(self, constraint_name):
            self.diag = SimpleNamespace(constraint_name=constraint_name)

    exact = IntegrityError("INSERT", {}, PgError("uq_availability_submission"))
    other = IntegrityError("INSERT", {}, PgError("some_other_constraint"))
    wrong_state = IntegrityError(
        "INSERT",
        {},
        SimpleNamespace(
            sqlstate="23503",
            diag=SimpleNamespace(constraint_name="uq_availability_submission"),
        ),
    )
    assert _submission_conflict(exact)
    assert not _submission_conflict(other)
    assert not _submission_conflict(wrong_state)
