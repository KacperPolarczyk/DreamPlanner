"""HTTP and query contract checks for monthly schedule reads."""

import base64
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.auth import session_service
from src.core.database import Base, get_session
from src.main import create_app
from src.schedules.models import Schedule

PATH = "/api/v1/schedules"
LARGE_ID = 9007199254740993
CREATOR_ID = 9007199254740995
NOW = datetime(2026, 10, 24, 10, tzinfo=UTC)


@pytest.fixture
def schedules_api(monkeypatch: pytest.MonkeyPatch):
    """Use a disposable database with real schedule queries and Bearer checks."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "schedule-reads-test-v1")
    monkeypatch.setenv("JWT_ISSUER", "dreamplanner-schedule-reads-test")
    monkeypatch.setenv("JWT_AUDIENCE", "dreamplanner-schedule-reads-api")
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[Schedule.__table__])
    with Session(engine) as session:
        session.add_all(
            [
                Schedule(
                    id=10,
                    year=2026,
                    month=11,
                    status="draft",
                    created_by=CREATOR_ID,
                    created_at=NOW,
                ),
                Schedule(
                    id=11,
                    year=2027,
                    month=1,
                    status="collecting",
                    created_by=CREATOR_ID,
                    created_at=NOW,
                    collection_opened_at=NOW + timedelta(hours=1),
                ),
                Schedule(
                    id=12,
                    year=2026,
                    month=12,
                    status="collection_closed",
                    created_by=CREATOR_ID,
                    created_at=NOW,
                    collection_opened_at=NOW + timedelta(hours=1),
                    collection_closed_at=NOW + timedelta(hours=2),
                ),
                Schedule(
                    id=13,
                    year=2027,
                    month=3,
                    status="closed",
                    created_by=CREATOR_ID,
                    created_at=NOW,
                ),
                Schedule(
                    id=LARGE_ID,
                    year=2027,
                    month=2,
                    status="published",
                    created_by=CREATOR_ID,
                    created_at=NOW,
                    published_at=NOW + timedelta(hours=3),
                ),
            ]
        )
        session.commit()

    app = create_app()
    session_id = uuid4()
    actor = SimpleNamespace(
        id=42,
        is_active=True,
        activated_at=datetime.now(UTC),
        role=SimpleNamespace(code="admin"),
    )
    auth_session = SimpleNamespace(
        user_id=42,
        revoked_at=None,
        idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
        absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
    )

    class AuthRepository:
        """Supply current identity for the real role dependencies."""

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
    yield app, token, actor, engine
    app.dependency_overrides.clear()
    engine.dispose()


def _headers(token: str) -> dict[str, str]:
    """Build a valid Bearer header for protected reads."""
    return {"Authorization": f"Bearer {token}"}


def _ids(response) -> list[str]:
    """Return the public IDs in a schedule page."""
    return [item["id"] for item in response.json()["items"]]


@pytest.mark.parametrize("role", ["admin", "manager"])
def test_list_and_detail_include_all_creators_and_statuses(schedules_api, role) -> None:
    app, token, actor, _ = schedules_api
    actor.role.code = role
    with TestClient(app) as client:
        listing = client.get(PATH, headers=_headers(token))
        detail = client.get(f"{PATH}/{LARGE_ID}", headers=_headers(token))
    assert listing.status_code == detail.status_code == 200
    assert listing.json()["total"] == 5
    assert listing.json()["limit"] == 25
    assert listing.json()["offset"] == 0
    assert _ids(listing) == ["13", str(LARGE_ID), "11", "12", "10"]
    assert detail.json()["created_by"] == str(CREATOR_ID)
    assert detail.json()["status"] == "published"
    assert listing.headers["cache-control"] == "no-store"
    assert detail.headers["cache-control"] == "no-store"


def test_empty_list_returns_empty_page(schedules_api) -> None:
    app, token, _, engine = schedules_api
    with Session(engine) as session:
        session.execute(delete(Schedule))
        session.commit()
    with TestClient(app) as client:
        response = client.get(PATH, headers=_headers(token))
    assert response.status_code == 200
    assert response.json() == {"items": [], "limit": 25, "offset": 0, "total": 0}


def test_pagination_keeps_total_before_limit_and_offset(schedules_api) -> None:
    app, token, _, _ = schedules_api
    with TestClient(app) as client:
        page = client.get(
            PATH, params={"limit": 2, "offset": 1}, headers=_headers(token)
        )
        beyond = client.get(
            PATH, params={"limit": 2, "offset": 20}, headers=_headers(token)
        )
    assert _ids(page) == [str(LARGE_ID), "11"]
    assert page.json()["total"] == 5
    assert page.json()["limit"] == 2
    assert page.json()["offset"] == 1
    assert beyond.json()["items"] == []
    assert beyond.json()["total"] == 5


def test_default_query_uses_descending_id_tie_breaker(schedules_api) -> None:
    """The unique period prevents visible ties, so inspect the generated order."""
    app, token, _, engine = schedules_api
    statements = []

    def capture_query(connection, cursor, statement, parameters, context, executemany):
        if "ORDER BY schedules.year" in statement:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_query)
    try:
        with TestClient(app) as client:
            response = client.get(PATH, headers=_headers(token))
    finally:
        event.remove(engine, "before_cursor_execute", capture_query)
    assert response.status_code == 200
    assert len(statements) == 1
    assert (
        "ORDER BY schedules.year DESC, schedules.month DESC, schedules.id DESC"
        in (statements[0])
    )


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("year", ["10", "12", "11", str(LARGE_ID), "13"]),
        ("-year", ["13", str(LARGE_ID), "11", "12", "10"]),
        ("month", ["11", str(LARGE_ID), "13", "10", "12"]),
        ("-month", ["12", "10", "13", str(LARGE_ID), "11"]),
    ],
)
def test_supported_sort_orders(schedules_api, sort, expected) -> None:
    app, token, _, _ = schedules_api
    with TestClient(app) as client:
        response = client.get(PATH, params={"sort": sort}, headers=_headers(token))
    assert response.status_code == 200
    assert _ids(response) == expected


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"year": 2027}, ["13", str(LARGE_ID), "11"]),
        ({"month": 11}, ["10"]),
        ({"status": "published"}, [str(LARGE_ID)]),
        ({"year": 2027, "month": 2, "status": "published"}, [str(LARGE_ID)]),
        ({"year": 2026, "month": 2, "status": "published"}, []),
    ],
)
def test_filters_can_be_combined(schedules_api, params, expected) -> None:
    app, token, _, _ = schedules_api
    with TestClient(app) as client:
        response = client.get(PATH, params=params, headers=_headers(token))
    assert response.status_code == 200
    assert _ids(response) == expected
    assert response.json()["total"] == len(expected)


@pytest.mark.parametrize(
    ("path", "params"),
    [
        (PATH, {"q": "text"}),
        (PATH, {"year": "1999"}),
        (PATH, {"year": "2101"}),
        (PATH, {"year": "2027.0"}),
        (PATH, {"month": "0"}),
        (PATH, {"month": "13"}),
        (PATH, {"status": "unknown"}),
        (PATH, {"sort": "id"}),
        (PATH, {"limit": "0"}),
        (PATH, {"limit": "101"}),
        (PATH, {"offset": "-1"}),
        (PATH, {"offset": "9223372036854775808"}),
        (PATH, [("year", "2027"), ("year", "2026")]),
        (f"{PATH}/invalid", None),
        (f"{PATH}/-1", None),
        (f"{PATH}/9223372036854775808", None),
    ],
)
def test_invalid_inputs_return_422_problem(schedules_api, path, params) -> None:
    app, token, _, _ = schedules_api
    with TestClient(app) as client:
        response = client.get(path, params=params, headers=_headers(token))
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert response.json()["errors"]


@pytest.mark.parametrize("schedule_id", [10, 11, 12, 13, LARGE_ID])
def test_detail_returns_only_public_fields_in_every_status(
    schedules_api, schedule_id
) -> None:
    app, token, _, _ = schedules_api
    with TestClient(app) as client:
        response = client.get(f"{PATH}/{schedule_id}", headers=_headers(token))
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "id",
        "year",
        "month",
        "status",
        "created_by",
        "created_at",
        "collection_opened_at",
        "collection_closed_at",
        "published_at",
    }
    assert payload["id"] == str(schedule_id)
    assert payload["created_by"] == str(CREATOR_ID)
    assert payload["created_at"] == "2026-10-24T10:00:00Z"
    assert payload["collection_opened_at"] in {None, "2026-10-24T11:00:00Z"}
    assert payload["collection_closed_at"] in {None, "2026-10-24T12:00:00Z"}
    assert payload["published_at"] in {None, "2026-10-24T13:00:00Z"}


def test_missing_schedule_and_canonical_id(schedules_api) -> None:
    app, token, _, _ = schedules_api
    with TestClient(app) as client:
        missing = client.get(f"{PATH}/999", headers=_headers(token))
        zero = client.get(f"{PATH}/0", headers=_headers(token))
        leading_zero = client.get(f"{PATH}/0010", headers=_headers(token))
    assert missing.status_code == zero.status_code == 404
    assert missing.headers["content-type"] == "application/problem+json"
    assert missing.headers["cache-control"] == "no-store"
    assert missing.json()["type"] == "about:blank"
    assert leading_zero.status_code == 200
    assert leading_zero.json()["id"] == "10"


def test_employee_and_unauthenticated_callers_cannot_read(schedules_api) -> None:
    app, token, actor, _ = schedules_api
    actor.role.code = "employee"
    with TestClient(app) as client:
        forbidden = [
            client.get(PATH, headers=_headers(token)),
            client.get(f"{PATH}/999", headers=_headers(token)),
        ]
        unauthenticated = [client.get(PATH), client.get(f"{PATH}/10")]
    assert [response.status_code for response in forbidden] == [403, 403]
    assert [response.status_code for response in unauthenticated] == [401, 401]
    assert all(
        response.headers["content-type"] == "application/problem+json"
        for response in forbidden + unauthenticated
    )
    assert all(
        response.headers["www-authenticate"] == "Bearer" for response in unauthenticated
    )


def test_openapi_documents_schedule_reads(schedules_api) -> None:
    app, _, _, _ = schedules_api
    paths = app.openapi()["paths"]
    assert set(paths[PATH]) == {"get", "post"}
    assert set(paths[f"{PATH}/{{schedule_id}}"]) == {"get", "delete"}
    assert {path for path in paths if path.startswith(PATH)} == {
        PATH,
        f"{PATH}/{{schedule_id}}",
        f"{PATH}/{{schedule_id}}/shift-requirements",
        f"{PATH}/{{schedule_id}}/shift-requirements/{{requirement_id}}",
        f"{PATH}/{{schedule_id}}/collection-opening",
        f"{PATH}/{{schedule_id}}/collection-closing",
        f"{PATH}/{{schedule_id}}/closing",
        f"{PATH}/{{schedule_id}}/availability-submissions",
        f"{PATH}/{{schedule_id}}/availability-submissions/{{submission_id}}",
        f"{PATH}/{{schedule_id}}/shift-assignments",
        f"{PATH}/{{schedule_id}}/shift-assignments/{{assignment_id}}",
    }
    for path, statuses, result in (
        (PATH, {"200", "401", "403", "422"}, "ScheduleListResponse"),
        (
            f"{PATH}/{{schedule_id}}",
            {"200", "401", "403", "404", "422"},
            "ScheduleResponse",
        ),
    ):
        operation = paths[path]["get"]
        assert operation["security"] == [{"BearerAuth": []}]
        assert statuses <= set(operation["responses"])
        assert operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ] == {"$ref": f"#/components/schemas/{result}"}
        for status in statuses - {"200"}:
            assert (
                "application/problem+json" in operation["responses"][status]["content"]
            )
    query_parameters = {
        parameter["name"]
        for parameter in paths[PATH]["get"]["parameters"]
        if parameter["in"] == "query"
    }
    assert query_parameters == {"year", "month", "status", "sort", "limit", "offset"}
    schedule_schema = app.openapi()["components"]["schemas"]["ScheduleResponse"]
    assert set(schedule_schema["properties"]) == {
        "id",
        "year",
        "month",
        "status",
        "created_by",
        "created_at",
        "collection_opened_at",
        "collection_closed_at",
        "published_at",
    }
