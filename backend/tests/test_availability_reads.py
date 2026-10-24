"""HTTP, ownership, and query contract checks for availability reads."""

import base64
import secrets
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.auth import session_service
from src.availability.models import AvailabilityEntry, AvailabilitySubmission
from src.core.database import Base, get_session
from src.main import create_app
from src.schedules.models import Schedule

SCHEDULE_ID = 45
OTHER_SCHEDULE_ID = 46
PATH = f"/api/v1/schedules/{SCHEDULE_ID}/availability-submissions"
NOW = datetime(2026, 10, 24, 10, tzinfo=UTC)
LARGE_ID = 9007199254740993


@pytest.fixture
def availability_api(monkeypatch: pytest.MonkeyPatch):
    """Use SQLite records and real Bearer authentication and role dependencies."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "availability-reads-test-v1")
    monkeypatch.setenv("JWT_ISSUER", "dreamplanner-availability-reads-test")
    monkeypatch.setenv("JWT_AUDIENCE", "dreamplanner-availability-reads-api")
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def disable_sqlite_fk(connection, record) -> None:
        """SQLite does not need PostgreSQL FK enforcement for these read tests."""
        connection.execute("PRAGMA foreign_keys=OFF")

    Base.metadata.create_all(
        engine,
        tables=[
            Schedule.__table__,
            AvailabilitySubmission.__table__,
            AvailabilityEntry.__table__,
        ],
    )
    with Session(engine) as session:
        schedules = [(SCHEDULE_ID, 2026, 11, "draft")]
        schedules.append((OTHER_SCHEDULE_ID, 2026, 12, "collecting"))
        session.add_all(
            [
                Schedule(
                    id=schedule_id,
                    year=year,
                    month=month,
                    status=status,
                    created_by=100,
                    created_at=NOW,
                )
                for schedule_id, year, month, status in schedules
            ]
        )
        session.flush()
        session.add_all(
            [
                AvailabilitySubmission(
                    id=101,
                    schedule_id=SCHEDULE_ID,
                    user_id=42,
                    submitted_at=None,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                AvailabilitySubmission(
                    id=102,
                    schedule_id=SCHEDULE_ID,
                    user_id=77,
                    submitted_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                AvailabilitySubmission(
                    id=103,
                    schedule_id=SCHEDULE_ID,
                    user_id=88,
                    submitted_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                AvailabilitySubmission(
                    id=104,
                    schedule_id=SCHEDULE_ID,
                    user_id=99,
                    submitted_at=None,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                AvailabilitySubmission(
                    id=105,
                    schedule_id=OTHER_SCHEDULE_ID,
                    user_id=42,
                    submitted_at=None,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                AvailabilitySubmission(
                    id=LARGE_ID,
                    schedule_id=SCHEDULE_ID,
                    user_id=9007199254740995,
                    submitted_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                AvailabilityEntry(
                    id=303,
                    submission_id=101,
                    work_date=date(2026, 11, 5),
                    start_time=time(9),
                    end_time=time(12),
                    status="available",
                    note=None,
                ),
                AvailabilityEntry(
                    id=302,
                    submission_id=101,
                    work_date=date(2026, 11, 4),
                    start_time=time(12),
                    end_time=time(14),
                    status="unavailable",
                    note="Afternoon",
                ),
                AvailabilityEntry(
                    id=301,
                    submission_id=101,
                    work_date=date(2026, 11, 4),
                    start_time=time(8),
                    end_time=time(10),
                    status="available",
                    note=None,
                ),
            ]
        )
        session.commit()

    app = create_app()
    session_id = uuid4()
    actor = SimpleNamespace(
        id=42,
        is_active=True,
        activated_at=NOW,
        role=SimpleNamespace(code="manager"),
    )
    auth_session = SimpleNamespace(
        user_id=42,
        revoked_at=None,
        idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
        absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
    )

    class AuthRepository:
        """Provide current identity values to the real authentication layer."""

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


def _ids(response) -> list[str]:
    return [item["id"] for item in response.json()["items"]]


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_manager_and_admin_list_all_visible_submissions(availability_api, role) -> None:
    app, token, actor, _ = availability_api
    actor.role.code = role
    with TestClient(app) as client:
        response = client.get(PATH, headers=_headers(token))
    assert response.status_code == 200
    assert _ids(response) == ["101", "102", "103", "104", str(LARGE_ID)]
    assert response.json()["total"] == 5
    assert response.headers["cache-control"] == "no-store"
    assert "entries" not in response.json()["items"][0]


def test_employee_list_is_scoped_in_query_and_total(availability_api) -> None:
    app, token, actor, _ = availability_api
    actor.role.code = "employee"
    with TestClient(app) as client:
        response = client.get(PATH, headers=_headers(token))
    assert response.status_code == 200
    assert _ids(response) == ["101"]
    assert response.json()["total"] == 1
    assert all(item["user_id"] == "42" for item in response.json()["items"])
    with TestClient(app) as client:
        schedules_response = client.get("/api/v1/schedules", headers=_headers(token))
    assert schedules_response.status_code == 403


def test_employee_with_no_submission_sees_empty_page(availability_api) -> None:
    app, token, actor, engine = availability_api
    actor.role.code = "employee"
    with Session(engine) as session:
        session.execute(
            delete(AvailabilitySubmission).where(AvailabilitySubmission.user_id == 42)
        )
        session.commit()
    with TestClient(app) as client:
        response = client.get(PATH, headers=_headers(token))
    assert response.status_code == 200
    assert response.json() == {"items": [], "limit": 25, "offset": 0, "total": 0}


def test_manager_filters_user_and_state(availability_api) -> None:
    app, token, _, _ = availability_api
    with TestClient(app) as client:
        by_user = client.get(PATH, params={"user_id": "77"}, headers=_headers(token))
        by_state = client.get(PATH, params={"state": "draft"}, headers=_headers(token))
        combined = client.get(
            PATH,
            params={"user_id": "99", "state": "draft"},
            headers=_headers(token),
        )
    assert _ids(by_user) == ["102"]
    assert by_user.json()["total"] == 1
    assert _ids(by_state) == ["101", "104"]
    assert by_state.json()["total"] == 2
    assert _ids(combined) == ["104"]
    assert combined.json()["total"] == 1


def test_employee_state_filter_and_pagination_are_scoped(availability_api) -> None:
    app, token, actor, _ = availability_api
    actor.role.code = "employee"
    with TestClient(app) as client:
        listing = client.get(
            PATH,
            params={"state": "draft", "limit": "1", "offset": "0"},
            headers=_headers(token),
        )
    assert _ids(listing) == ["101"]
    assert listing.json()["total"] == 1
    assert listing.json()["limit"] == 1


def test_pagination_total_precedes_page_and_sort_is_stable(availability_api) -> None:
    app, token, _, _ = availability_api
    with TestClient(app) as client:
        page = client.get(
            PATH, params={"limit": "2", "offset": "1"}, headers=_headers(token)
        )
    assert page.status_code == 200
    assert _ids(page) == ["102", "103"]
    assert page.json()["total"] == 5
    assert page.json()["limit"] == 2
    assert page.json()["offset"] == 1


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_manager_and_admin_detail_include_any_schedule_submission(
    availability_api, role
) -> None:
    app, token, actor, _ = availability_api
    actor.role.code = role
    with TestClient(app) as client:
        response = client.get(f"{PATH}/102", headers=_headers(token))
    assert response.status_code == 200
    assert response.json()["id"] == "102"
    assert response.json()["user_id"] == "77"
    assert response.headers["cache-control"] == "no-store"


def test_employee_reads_own_detail_with_string_bigints(availability_api) -> None:
    app, token, actor, _ = availability_api
    actor.role.code = "employee"
    with TestClient(app) as client:
        response = client.get(f"{PATH}/101", headers=_headers(token))
    assert response.status_code == 200
    assert response.json() == {
        "id": "101",
        "schedule_id": str(SCHEDULE_ID),
        "user_id": "42",
        "submitted_at": None,
        "created_at": "2026-10-24T10:00:00Z",
        "updated_at": "2026-10-24T10:00:00Z",
        "entries": [
            {
                "id": "301",
                "work_date": "2026-11-04",
                "start_time": "08:00:00",
                "end_time": "10:00:00",
                "status": "available",
                "note": None,
            },
            {
                "id": "302",
                "work_date": "2026-11-04",
                "start_time": "12:00:00",
                "end_time": "14:00:00",
                "status": "unavailable",
                "note": "Afternoon",
            },
            {
                "id": "303",
                "work_date": "2026-11-05",
                "start_time": "09:00:00",
                "end_time": "12:00:00",
                "status": "available",
                "note": None,
            },
        ],
    }
    assert response.headers["cache-control"] == "no-store"


def test_detail_can_serialize_bigint_ids_above_javascript_safe_range(availability_api):
    app, token, _, _ = availability_api
    with TestClient(app) as client:
        response = client.get(f"{PATH}/{LARGE_ID}", headers=_headers(token))
    assert response.status_code == 200
    assert response.json()["id"] == str(LARGE_ID)
    assert response.json()["user_id"] == "9007199254740995"


def test_employee_cannot_read_other_users_detail(availability_api) -> None:
    app, token, actor, _ = availability_api
    actor.role.code = "employee"
    with TestClient(app) as client:
        response = client.get(f"{PATH}/102", headers=_headers(token))
    assert response.status_code == 404
    assert response.json()["detail"] == "Zasób nie istnieje."


@pytest.mark.parametrize(
    "path",
    [
        f"{PATH}/999",
        f"{PATH}/102",
        f"/api/v1/schedules/{OTHER_SCHEDULE_ID}/availability-submissions/101",
    ],
)
def test_missing_wrong_schedule_or_foreign_detail_is_404(
    availability_api, path
) -> None:
    app, token, actor, _ = availability_api
    actor.role.code = "employee"
    with TestClient(app) as client:
        response = client.get(path, headers=_headers(token))
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "status", ["draft", "collecting", "collection_closed", "closed", "published"]
)
def test_list_and_detail_work_in_every_schedule_status(
    availability_api, status
) -> None:
    app, token, actor, engine = availability_api
    actor.role.code = "manager"
    with Session(engine) as session:
        schedule = session.get(Schedule, SCHEDULE_ID)
        schedule.status = status
        session.commit()
    with TestClient(app) as client:
        listing = client.get(PATH, headers=_headers(token))
        detail = client.get(f"{PATH}/101", headers=_headers(token))
    assert listing.status_code == detail.status_code == 200


def test_missing_schedule_is_404(availability_api) -> None:
    app, token, _, _ = availability_api
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/schedules/999/availability-submissions", headers=_headers(token)
        )
    assert response.status_code == 404


def test_zero_path_ids_are_valid_but_missing_resources(availability_api) -> None:
    app, token, _, _ = availability_api
    with TestClient(app) as client:
        listing = client.get(
            "/api/v1/schedules/0/availability-submissions", headers=_headers(token)
        )
        detail = client.get(f"{PATH}/0", headers=_headers(token))
    assert listing.status_code == detail.status_code == 404


def test_missing_bearer_token_is_401(availability_api) -> None:
    app, _, _, _ = availability_api
    with TestClient(app) as client:
        response = client.get(PATH)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"


def test_role_is_checked_before_schedule_lookup(availability_api, monkeypatch) -> None:
    app, token, actor, _ = availability_api
    actor.role.code = "unknown"

    def fail_lookup(self, schedule_id):
        raise AssertionError("schedule lookup ran before role validation")

    monkeypatch.setattr(
        "src.availability.service.ScheduleRepository.get_by_id", fail_lookup
    )
    with TestClient(app) as client:
        response = client.get(f"{PATH}/999", headers=_headers(token))
    assert response.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/schedules/+1/availability-submissions",
        "/api/v1/schedules/-1/availability-submissions",
        "/api/v1/schedules/１２/availability-submissions",
        "/api/v1/schedules/9223372036854775808/availability-submissions",
        f"{PATH}/9223372036854775808",
    ],
)
def test_invalid_path_ids_return_validation_problem(availability_api, path) -> None:
    app, token, _, _ = availability_api
    with TestClient(app) as client:
        response = client.get(path, headers=_headers(token))
    assert response.status_code == 422
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "params",
    [
        "limit=0",
        "limit=101",
        "limit=1.5",
        "limit=+1",
        "limit=true",
        "offset=-1",
        "offset=9223372036854775808",
        "offset=１",
        f"offset={'9' * 5000}",
        "user_id=0",
        "user_id=-1",
        "user_id=9223372036854775808",
        "user_id=abc",
        "state=unknown",
        "sort=user_id",
        "limit=1&limit=2",
        "state=draft&state=submitted",
        "user_id=42&unknown=x",
    ],
)
def test_invalid_or_repeated_query_returns_422(availability_api, params) -> None:
    app, token, _, _ = availability_api
    with TestClient(app) as client:
        response = client.get(f"{PATH}?{params}", headers=_headers(token))
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert response.json()["errors"]


@pytest.mark.parametrize("role", ["employee", "manager", "admin"])
def test_user_filter_permissions(availability_api, role) -> None:
    app, token, actor, _ = availability_api
    actor.role.code = role
    with TestClient(app) as client:
        response = client.get(f"{PATH}?user_id=42", headers=_headers(token))
    assert response.status_code == (403 if role == "employee" else 200)


def test_employee_user_filter_is_forbidden_even_when_repeated(availability_api) -> None:
    app, token, actor, _ = availability_api
    actor.role.code = "employee"
    with TestClient(app) as client:
        response = client.get(f"{PATH}?user_id=42&user_id=42", headers=_headers(token))
    assert response.status_code == 403


def test_openapi_documents_availability_endpoints_and_query_contract(availability_api):
    app, _, _, _ = availability_api
    paths = app.openapi()["paths"]
    list_path = "/api/v1/schedules/{schedule_id}/availability-submissions"
    detail_path = f"{list_path}/{{submission_id}}"
    assert set(paths[list_path]) == {"get", "post"}
    assert set(paths[detail_path]) == {"get"}
    operation = paths[list_path]["get"]
    assert operation["security"] == [{"BearerAuth": []}]
    assert {"200", "401", "403", "404", "422"} <= set(operation["responses"])
    assert {
        parameter["name"]
        for parameter in operation["parameters"]
        if parameter["in"] == "query"
    } == {"limit", "offset", "user_id", "state"}
    create_operation = paths[list_path]["post"]
    assert create_operation["security"] == [{"BearerAuth": []}]
    assert "201" in create_operation["responses"]
    assert "requestBody" in create_operation
    detail_schema = app.openapi()["components"]["schemas"][
        "AvailabilitySubmissionResponse"
    ]
    assert set(detail_schema["properties"]) == {
        "id",
        "schedule_id",
        "user_id",
        "submitted_at",
        "created_at",
        "updated_at",
        "entries",
    }
    summary_schema = app.openapi()["components"]["schemas"][
        "AvailabilitySubmissionSummary"
    ]
    assert set(summary_schema["properties"]) == {
        "id",
        "schedule_id",
        "user_id",
        "submitted_at",
        "created_at",
        "updated_at",
    }
