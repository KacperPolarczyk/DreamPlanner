"""HTTP contract tests for an employee's own published shifts."""

import base64
import secrets
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.assignments.models import ShiftAssignment
from src.auth import session_service
from src.core.database import Base, get_session
from src.main import create_app
from src.positions.models import Position
from src.schedules.models import Schedule, ShiftRequirement

PATH = "/api/v1/me/shifts"
EMPLOYEE_ID = 42
OTHER_EMPLOYEE_ID = 43
LARGE_ASSIGNMENT_ID = 9007199254740993
NOW = datetime(2026, 10, 24, 10, tzinfo=UTC)


@pytest.fixture
def my_shifts_api(monkeypatch: pytest.MonkeyPatch):
    """Use real JWT/RBAC dependencies and an isolated in-memory database."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "my-shifts-v1")
    monkeypatch.setenv("JWT_ISSUER", "my-shifts")
    monkeypatch.setenv("JWT_AUDIENCE", "my-shifts-api")
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, record) -> None:
        connection.create_function("btrim", 1, str.strip)
        connection.execute("PRAGMA foreign_keys=OFF")

    Base.metadata.create_all(
        engine,
        tables=[
            Position.__table__,
            Schedule.__table__,
            ShiftRequirement.__table__,
            ShiftAssignment.__table__,
        ],
    )
    with Session(engine) as session:
        session.add(Position(id=1, name="Front desk", is_active=True))
        statuses = [
            (1, 2026, 12, "published"),
            (2, 2026, 11, "draft"),
            (3, 2027, 1, "collecting"),
            (4, 2027, 2, "collection_closed"),
            (5, 2027, 3, "closed"),
        ]
        session.add_all(
            [
                Schedule(
                    id=schedule_id,
                    year=year,
                    month=month,
                    status=status,
                    created_by=7,
                    created_at=NOW,
                )
                for schedule_id, year, month, status in statuses
            ]
        )
        session.flush()
        requirement_data = [
            (31, 1, date(2026, 12, 1), time(12), time(16)),
            (32, 1, date(2026, 12, 1), time(8), time(12)),
            (33, 1, date(2026, 12, 1), time(8), time(10)),
            (34, 1, date(2026, 12, 2), time(9), time(17)),
            (35, 2, date(2026, 11, 2), time(8), time(16)),
            (36, 3, date(2027, 1, 2), time(8), time(16)),
            (37, 4, date(2027, 2, 2), time(8), time(16)),
            (38, 5, date(2027, 3, 2), time(8), time(16)),
        ]
        session.add_all(
            [
                ShiftRequirement(
                    id=rid,
                    schedule_id=schedule_id,
                    position_id=1,
                    work_date=work_date,
                    start_time=start,
                    end_time=end,
                    required_workers=2,
                )
                for rid, schedule_id, work_date, start, end in requirement_data
            ]
        )
        session.flush()
        session.add_all(
            [
                ShiftAssignment(
                    id=assignment_id,
                    shift_requirement_id=requirement_id,
                    user_id=user_id,
                    assigned_by=7,
                    assigned_at=NOW,
                    warning_type=warning,
                    warning_acknowledged_at=NOW if warning else None,
                )
                for assignment_id, requirement_id, user_id, warning in [
                    (101, 31, EMPLOYEE_ID, None),
                    (102, 32, EMPLOYEE_ID, "unavailable"),
                    (103, 33, EMPLOYEE_ID, "unknown"),
                    (LARGE_ASSIGNMENT_ID, 34, EMPLOYEE_ID, None),
                    (105, 31, OTHER_EMPLOYEE_ID, None),
                    (106, 35, EMPLOYEE_ID, None),
                    (107, 36, EMPLOYEE_ID, None),
                    (108, 37, EMPLOYEE_ID, None),
                    (109, 38, EMPLOYEE_ID, None),
                ]
            ]
        )
        session.commit()

    app = create_app()
    session_id = uuid4()
    actor = SimpleNamespace(
        id=EMPLOYEE_ID,
        is_active=True,
        activated_at=NOW,
        role=SimpleNamespace(code="employee"),
    )
    auth_session = SimpleNamespace(
        user_id=EMPLOYEE_ID,
        revoked_at=None,
        idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
        absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
    )

    class AuthRepository:
        def get_session(self, requested_id):
            return auth_session if requested_id == session_id else None

        def get_user_fresh(self, requested_id):
            return actor if requested_id == EMPLOYEE_ID else None

    monkeypatch.setattr(
        session_service, "AuthSessionRepository", lambda session: AuthRepository()
    )

    def test_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    token = app.state.jwt_codec.issue(EMPLOYEE_ID, session_id)
    try:
        yield app, token, actor, auth_session, engine
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _items(client: TestClient, token: str, **params):
    return client.get(PATH, params=params, headers=_headers(token))


def test_list_only_own_published_shifts_and_safe_fields(my_shifts_api) -> None:
    app, token, _, _, _ = my_shifts_api
    with TestClient(app) as client:
        response = _items(client, token)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["total"] == 4
    assert [item["assignment_id"] for item in body["items"]] == [
        "102",
        "103",
        "101",
        str(LARGE_ASSIGNMENT_ID),
    ]
    assert set(body["items"][0]) == {
        "assignment_id",
        "schedule_id",
        "shift_requirement_id",
        "position_id",
        "position_name",
        "work_date",
        "start_time",
        "end_time",
        "warning_type",
        "warning_acknowledged_at",
    }
    assert body["items"][0] == {
        "assignment_id": "102",
        "schedule_id": "1",
        "shift_requirement_id": "32",
        "position_id": "1",
        "position_name": "Front desk",
        "work_date": "2026-12-01",
        "start_time": "08:00:00",
        "end_time": "12:00:00",
        "warning_type": "unavailable",
        "warning_acknowledged_at": "2026-10-24T10:00:00Z",
    }
    assert body["items"][1]["warning_type"] == "unknown"
    assert body["items"][1]["warning_acknowledged_at"] == "2026-10-24T10:00:00Z"
    assert all(
        "user_id" not in item and "assigned_by" not in item for item in body["items"]
    )


def test_filters_are_inclusive_and_total_matches_scope(my_shifts_api) -> None:
    app, token, _, _, _ = my_shifts_api
    with TestClient(app) as client:
        from_only = _items(client, token, from_date="2026-12-02")
        to_only = _items(client, token, to_date="2026-12-01")
        both = _items(
            client,
            token,
            from_date="2026-12-01",
            to_date="2026-12-01",
        )
        one_date = _items(
            client,
            token,
            from_date="2026-12-02",
            to_date="2026-12-02",
        )
    assert from_only.json()["total"] == 1
    assert [item["assignment_id"] for item in from_only.json()["items"]] == [
        str(LARGE_ASSIGNMENT_ID)
    ]
    assert to_only.json()["total"] == 3
    assert both.json()["total"] == 3
    assert one_date.json()["total"] == 1


@pytest.mark.parametrize(
    "params",
    [
        {"from_date": "2026-12-03", "to_date": "2026-12-01"},
        {"from_date": "2026/12/01"},
        {"to_date": "2026-02-30"},
        {"limit": "0"},
        {"limit": "101"},
        {"offset": "-1"},
        {"user_id": str(EMPLOYEE_ID)},
        {"sort": "-work_date"},
    ],
)
def test_invalid_query_returns_problem_details(my_shifts_api, params) -> None:
    app, token, _, _, _ = my_shifts_api
    with TestClient(app) as client:
        response = _items(client, token, **params)
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"


def test_pagination_boundaries_and_empty_page(my_shifts_api) -> None:
    app, token, _, _, _ = my_shifts_api
    with TestClient(app) as client:
        first = _items(client, token, limit="1")
        maximum = _items(client, token, limit="100")
        past_end = _items(client, token, offset="99")
        empty = _items(client, token, from_date="2030-01-01")
    assert first.json()["items"][0]["assignment_id"] == "102"
    assert first.json()["total"] == 4
    assert maximum.json()["total"] == 4
    assert past_end.json() == {"items": [], "limit": 25, "offset": 99, "total": 4}
    assert empty.json() == {"items": [], "limit": 25, "offset": 0, "total": 0}


@pytest.mark.parametrize("assignment_id", [105, 106, 107, 108, 109, 999])
def test_detail_hides_foreign_unpublished_and_unknown_assignments(
    my_shifts_api, assignment_id
) -> None:
    app, token, _, _, _ = my_shifts_api
    with TestClient(app) as client:
        response = client.get(f"{PATH}/{assignment_id}", headers=_headers(token))
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": "about:blank",
        "title": "Nie znaleziono przydziału",
        "status": 404,
        "detail": "Zasób nie istnieje.",
    }


def test_detail_returns_only_own_published_shift(my_shifts_api) -> None:
    app, token, _, _, _ = my_shifts_api
    with TestClient(app) as client:
        response = client.get(f"{PATH}/{LARGE_ASSIGNMENT_ID}", headers=_headers(token))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["assignment_id"] == str(LARGE_ASSIGNMENT_ID)
    assert response.json()["warning_type"] is None
    assert response.json()["warning_acknowledged_at"] is None
    assert "user_id" not in response.json()


def test_authentication_role_and_active_account(my_shifts_api) -> None:
    app, token, actor, auth_session, _ = my_shifts_api
    with TestClient(app) as client:
        missing = client.get(PATH)
        invalid = client.get(PATH, headers=_headers("invalid"))
        missing_detail = client.get(f"{PATH}/101")
        invalid_detail = client.get(f"{PATH}/101", headers=_headers("invalid"))
        assert missing.status_code == invalid.status_code == 401
        assert missing_detail.status_code == invalid_detail.status_code == 401
        assert client.get(PATH, headers=_headers(token)).status_code == 200
        actor.role.code = "manager"
        manager = client.get(PATH, headers=_headers(token))
        manager_detail = client.get(f"{PATH}/101", headers=_headers(token))
        actor.role.code = "admin"
        admin = client.get(PATH, headers=_headers(token))
        admin_detail = client.get(f"{PATH}/101", headers=_headers(token))
        actor.role.code = "employee"
        actor.is_active = False
        inactive = client.get(PATH, headers=_headers(token))
        inactive_detail = client.get(f"{PATH}/101", headers=_headers(token))
        actor.is_active = True
        actor.activated_at = None
        not_activated = client.get(PATH, headers=_headers(token))
        not_activated_detail = client.get(f"{PATH}/101", headers=_headers(token))
        auth_session.revoked_at = datetime.now(UTC)
        revoked = client.get(PATH, headers=_headers(token))
        revoked_detail = client.get(f"{PATH}/101", headers=_headers(token))
    assert all(
        response.status_code == 403
        for response in (
            manager,
            manager_detail,
            admin,
            admin_detail,
            inactive,
            inactive_detail,
            not_activated,
            not_activated_detail,
        )
    )
    assert revoked.status_code == 401
    assert revoked_detail.status_code == 401
    assert all(
        response.headers["cache-control"] == "no-store"
        for response in (
            missing,
            invalid,
            missing_detail,
            invalid_detail,
            manager,
            manager_detail,
            admin,
            admin_detail,
            inactive,
            inactive_detail,
            not_activated,
            not_activated_detail,
            revoked,
            revoked_detail,
        )
    )


def test_bad_assignment_id_and_openapi(my_shifts_api) -> None:
    app, token, _, _, _ = my_shifts_api
    with TestClient(app) as client:
        invalid_id = client.get(f"{PATH}/9223372036854775808", headers=_headers(token))
    assert invalid_id.status_code == 422
    assert invalid_id.headers["cache-control"] == "no-store"
    schema = app.openapi()
    listing = schema["paths"][PATH]["get"]
    detail = schema["paths"][f"{PATH}/{{assignment_id}}"]["get"]
    assert listing["security"] == detail["security"] == [{"BearerAuth": []}]
    assert "user_id" not in {parameter["name"] for parameter in listing["parameters"]}
    assert {"from_date", "to_date", "limit", "offset"} <= {
        parameter["name"] for parameter in listing["parameters"]
    }
    assert {"200", "401", "403", "422"} <= listing["responses"].keys()
    assert {"200", "401", "403", "404", "422"} <= detail["responses"].keys()
    assert detail["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/MyShiftResponse")
