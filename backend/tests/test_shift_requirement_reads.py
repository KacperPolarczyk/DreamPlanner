"""HTTP and query contract checks for shift requirement reads."""

import base64
import secrets
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.assignments.models import ShiftAssignment
from src.auth import session_service
from src.core.database import Base, get_session
from src.main import create_app
from src.positions.models import Position
from src.schedules.models import Schedule, ShiftRequirement
from src.schedules.repository import ScheduleRepository
from src.users.models import Role, User

PATH = "/api/v1/schedules"
SCHEDULE_ID = 45
OTHER_SCHEDULE_ID = 46
LARGE_ID = 9007199254740993
LARGE_POSITION_ID = 9007199254740997
LARGE_SCHEDULE_ID = 9007199254740995
NOW = datetime(2026, 10, 24, 10, tzinfo=UTC)


@pytest.fixture
def shift_requirements_api(monkeypatch: pytest.MonkeyPatch):
    """Use SQLite rows and the application's real Bearer role dependencies."""
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "shift-requirement-reads-test-v1")
    monkeypatch.setenv("JWT_ISSUER", "dreamplanner-shift-requirements-test")
    monkeypatch.setenv("JWT_AUDIENCE", "dreamplanner-shift-requirements-api")
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def register_btrim(connection, record) -> None:
        """Support the Position check constraint in SQLite."""
        connection.create_function("btrim", 1, str.strip)

    Base.metadata.create_all(
        engine,
        tables=[
            Role.__table__,
            User.__table__,
            Position.__table__,
            Schedule.__table__,
            ShiftRequirement.__table__,
            ShiftAssignment.__table__,
        ],
    )
    with Session(engine) as session:
        session.add(Role(id=1, code="manager", name="Manager"))
        session.add(
            User(
                id=42,
                role_id=1,
                email="manager@example.test",
                full_name="Manager",
                password_hash="unused",
                is_active=True,
                activated_at=NOW,
            )
        )
        session.add_all(
            [
                Position(id=7, name="Kasjer", is_active=True),
                Position(id=8, name="Magazyn", is_active=False),
                Position(id=LARGE_POSITION_ID, name="Duże ID", is_active=True),
            ]
        )
        statuses = [
            (SCHEDULE_ID, 2026, 11, "draft"),
            (OTHER_SCHEDULE_ID, 2026, 12, "collecting"),
            (47, 2027, 1, "collection_closed"),
            (48, 2027, 2, "closed"),
            (49, 2027, 3, "published"),
            (50, 2027, 4, "draft"),
            (51, 2028, 3, "draft"),
            (52, 2028, 10, "draft"),
        ]
        session.add_all(
            [
                Schedule(
                    id=schedule_id,
                    year=year,
                    month=month,
                    status=status,
                    created_by=9007199254740995,
                    created_at=NOW,
                )
                for schedule_id, year, month, status in statuses
            ]
        )
        session.add(
            Schedule(
                id=LARGE_SCHEDULE_ID,
                year=2027,
                month=5,
                status="draft",
                created_by=9007199254740995,
                created_at=NOW,
            )
        )
        session.flush()
        session.add_all(
            [
                ShiftRequirement(
                    id=LARGE_ID,
                    schedule_id=SCHEDULE_ID,
                    position_id=7,
                    work_date=date(2026, 11, 2),
                    start_time=time(8),
                    end_time=time(16),
                    required_workers=2,
                ),
                ShiftRequirement(
                    id=124,
                    schedule_id=SCHEDULE_ID,
                    position_id=8,
                    work_date=date(2026, 11, 2),
                    start_time=time(8),
                    end_time=time(16),
                    required_workers=1,
                    note="History",
                ),
                ShiftRequirement(
                    id=125,
                    schedule_id=SCHEDULE_ID,
                    position_id=7,
                    work_date=date(2026, 11, 1),
                    start_time=time(9),
                    end_time=time(12),
                    required_workers=1,
                ),
                ShiftRequirement(
                    id=126,
                    schedule_id=SCHEDULE_ID,
                    position_id=7,
                    work_date=date(2026, 11, 2),
                    start_time=time(7),
                    end_time=time(9),
                    required_workers=3,
                ),
                ShiftRequirement(
                    id=127,
                    schedule_id=OTHER_SCHEDULE_ID,
                    position_id=8,
                    work_date=date(2026, 12, 2),
                    start_time=time(8),
                    end_time=time(16),
                    required_workers=4,
                ),
                ShiftRequirement(
                    id=205,
                    schedule_id=LARGE_SCHEDULE_ID,
                    position_id=LARGE_POSITION_ID,
                    work_date=date(2027, 5, 2),
                    start_time=time(8),
                    end_time=time(16),
                    required_workers=1,
                ),
                ShiftRequirement(
                    id=206,
                    schedule_id=51,
                    position_id=7,
                    work_date=date(2028, 3, 2),
                    start_time=time(8),
                    end_time=time(16),
                    required_workers=1,
                ),
                ShiftRequirement(
                    id=207,
                    schedule_id=52,
                    position_id=7,
                    work_date=date(2028, 10, 2),
                    start_time=time(8),
                    end_time=time(16),
                    required_workers=1,
                ),
                *[
                    ShiftRequirement(
                        id=200 + index,
                        schedule_id=schedule_id,
                        position_id=7,
                        work_date=date(year, month, 2),
                        start_time=time(8),
                        end_time=time(16),
                        required_workers=1,
                    )
                    for index, (schedule_id, year, month, status) in enumerate(
                        statuses[1:5], start=1
                    )
                ],
            ]
        )
        session.commit()

    app = create_app()
    session_id = uuid4()
    actor = SimpleNamespace(
        id=42,
        is_active=True,
        activated_at=NOW,
        role=SimpleNamespace(code="admin"),
    )
    auth_session = SimpleNamespace(
        user_id=42,
        revoked_at=None,
        idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
        absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
    )

    class AuthRepository:
        """Return the fixture identity for the application's real auth check."""

        def get_session(self, requested_id):
            return auth_session if requested_id == session_id else None

        def get_user_fresh(self, requested_id):
            return actor if requested_id == 42 else None

    monkeypatch.setattr(
        session_service, "AuthSessionRepository", lambda session: AuthRepository()
    )

    def test_session():
        with Session(engine) as session:

            def sqlite_identity(session, flush_context, instances) -> None:
                """Emulate PostgreSQL BIGINT identity for endpoint tests."""
                for pending in session.new:
                    if isinstance(pending, ShiftRequirement) and pending.id is None:
                        current = session.scalar(select(func.max(ShiftRequirement.id)))
                        pending.id = (current or 0) + 1

            event.listen(session, "before_flush", sqlite_identity)
            try:
                yield session
            finally:
                event.remove(session, "before_flush", sqlite_identity)

    app.dependency_overrides[get_session] = test_session
    token = app.state.jwt_codec.issue(42, session_id)
    yield app, token, actor, engine
    app.dependency_overrides.clear()
    engine.dispose()


def _headers(token: str) -> dict[str, str]:
    """Build an authorized Bearer header."""
    return {"Authorization": f"Bearer {token}"}


def _list_path(schedule_id: int = SCHEDULE_ID) -> str:
    return f"{PATH}/{schedule_id}/shift-requirements"


def _detail_path(schedule_id: int, requirement_id: int) -> str:
    return f"{_list_path(schedule_id)}/{requirement_id}"


def test_list_reads_requirements_and_position_fields(shift_requirements_api) -> None:
    app, token, _, _ = shift_requirements_api
    with TestClient(app) as client:
        response = client.get(_list_path(), headers=_headers(token))
    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": "125",
                "schedule_id": "45",
                "position_id": "7",
                "position_name": "Kasjer",
                "position_is_active": True,
                "work_date": "2026-11-01",
                "start_time": "09:00:00",
                "end_time": "12:00:00",
                "required_workers": 1,
                "note": None,
            },
            {
                "id": "126",
                "schedule_id": "45",
                "position_id": "7",
                "position_name": "Kasjer",
                "position_is_active": True,
                "work_date": "2026-11-02",
                "start_time": "07:00:00",
                "end_time": "09:00:00",
                "required_workers": 3,
                "note": None,
            },
            {
                "id": "124",
                "schedule_id": "45",
                "position_id": "8",
                "position_name": "Magazyn",
                "position_is_active": False,
                "work_date": "2026-11-02",
                "start_time": "08:00:00",
                "end_time": "16:00:00",
                "required_workers": 1,
                "note": "History",
            },
            {
                "id": str(LARGE_ID),
                "schedule_id": "45",
                "position_id": "7",
                "position_name": "Kasjer",
                "position_is_active": True,
                "work_date": "2026-11-02",
                "start_time": "08:00:00",
                "end_time": "16:00:00",
                "required_workers": 2,
                "note": None,
            },
        ],
        "limit": 25,
        "offset": 0,
        "total": 4,
    }
    assert response.headers["cache-control"] == "no-store"


def test_list_empty_schedule_and_missing_schedule(shift_requirements_api) -> None:
    app, token, _, _ = shift_requirements_api
    with TestClient(app) as client:
        empty = client.get(_list_path(50), headers=_headers(token))
        missing = client.get(_list_path(999), headers=_headers(token))
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "limit": 25, "offset": 0, "total": 0}
    assert missing.status_code == 404
    assert missing.headers["content-type"] == "application/problem+json"
    assert missing.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("params", "expected_ids"),
    [
        ({"work_date": "2026-11-02"}, ["126", "124", str(LARGE_ID)]),
        ({"position_id": "8"}, ["124"]),
        (
            {"work_date": "2026-11-02", "position_id": "7"},
            ["126", str(LARGE_ID)],
        ),
        ({"position_id": "999999"}, []),
    ],
)
def test_list_filters(shift_requirements_api, params, expected_ids) -> None:
    app, token, _, _ = shift_requirements_api
    with TestClient(app) as client:
        response = client.get(_list_path(), params=params, headers=_headers(token))
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == expected_ids
    assert response.json()["total"] == len(expected_ids)


def test_list_pagination_keeps_total_before_limit_and_offset(
    shift_requirements_api,
) -> None:
    app, token, _, _ = shift_requirements_api
    with TestClient(app) as client:
        page = client.get(
            _list_path(), params={"limit": 2, "offset": 1}, headers=_headers(token)
        )
        beyond = client.get(
            _list_path(), params={"limit": 2, "offset": 20}, headers=_headers(token)
        )
    assert [item["id"] for item in page.json()["items"]] == ["126", "124"]
    assert page.json()["total"] == 4
    assert page.json()["limit"] == 2
    assert page.json()["offset"] == 1
    assert beyond.json() == {"items": [], "limit": 2, "offset": 20, "total": 4}


def test_list_does_not_leak_rows_from_other_schedules(shift_requirements_api) -> None:
    app, token, _, _ = shift_requirements_api
    with TestClient(app) as client:
        response = client.get(_list_path(), headers=_headers(token))
    assert response.status_code == 200
    assert all(
        item["schedule_id"] == str(SCHEDULE_ID) for item in response.json()["items"]
    )
    assert "127" not in [item["id"] for item in response.json()["items"]]


def test_list_uses_one_join_for_position_data(shift_requirements_api) -> None:
    app, token, _, engine = shift_requirements_api
    statements: list[str] = []

    def capture_query(connection, cursor, statement, parameters, context, executemany):
        if "FROM shift_requirements" in statement:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_query)
    try:
        with TestClient(app) as client:
            response = client.get(_list_path(), headers=_headers(token))
    finally:
        event.remove(engine, "before_cursor_execute", capture_query)
    assert response.status_code == 200
    data_queries = [
        statement for statement in statements if "JOIN positions" in statement
    ]
    assert len(data_queries) == 1


def test_detail_returns_scoped_requirement_and_large_ids_as_strings(
    shift_requirements_api,
) -> None:
    app, token, _, _ = shift_requirements_api
    with TestClient(app) as client:
        response = client.get(
            _detail_path(SCHEDULE_ID, LARGE_ID), headers=_headers(token)
        )
    assert response.status_code == 200
    assert response.json() == {
        "id": str(LARGE_ID),
        "schedule_id": str(SCHEDULE_ID),
        "position_id": "7",
        "position_name": "Kasjer",
        "position_is_active": True,
        "work_date": "2026-11-02",
        "start_time": "08:00:00",
        "end_time": "16:00:00",
        "required_workers": 2,
        "note": None,
    }
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("schedule_id", "requirement_id"),
    [(SCHEDULE_ID, 999), (999, LARGE_ID), (SCHEDULE_ID, 127)],
)
def test_detail_hides_missing_or_cross_schedule_requirement(
    shift_requirements_api, schedule_id, requirement_id
) -> None:
    app, token, _, _ = shift_requirements_api
    with TestClient(app) as client:
        response = client.get(
            _detail_path(schedule_id, requirement_id), headers=_headers(token)
        )
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("schedule_id", "requirement_id", "status"),
    [
        (SCHEDULE_ID, LARGE_ID, "draft"),
        (OTHER_SCHEDULE_ID, 201, "collecting"),
        (47, 202, "collection_closed"),
        (48, 203, "closed"),
        (49, 204, "published"),
    ],
)
def test_detail_reads_requirements_in_every_schedule_status(
    shift_requirements_api, schedule_id, requirement_id, status
) -> None:
    app, token, _, engine = shift_requirements_api
    with Session(engine) as session:
        assert session.get(Schedule, schedule_id).status == status
    with TestClient(app) as client:
        response = client.get(
            _detail_path(schedule_id, requirement_id), headers=_headers(token)
        )
    assert response.status_code == 200
    assert response.json()["schedule_id"] == str(schedule_id)
    assert response.json()["position_name"] == "Kasjer"


def test_detail_includes_inactive_position(shift_requirements_api) -> None:
    app, token, _, _ = shift_requirements_api
    with TestClient(app) as client:
        response = client.get(_detail_path(SCHEDULE_ID, 124), headers=_headers(token))
    assert response.status_code == 200
    assert response.json()["position_name"] == "Magazyn"
    assert response.json()["position_is_active"] is False


def test_detail_serializes_every_bigint_identifier_as_a_string(
    shift_requirements_api,
) -> None:
    app, token, _, _ = shift_requirements_api
    with TestClient(app) as client:
        response = client.get(
            _detail_path(LARGE_SCHEDULE_ID, 205), headers=_headers(token)
        )
    assert response.status_code == 200
    assert response.json()["id"] == "205"
    assert response.json()["schedule_id"] == str(LARGE_SCHEDULE_ID)
    assert response.json()["position_id"] == str(LARGE_POSITION_ID)


@pytest.mark.parametrize(
    ("schedule_id", "requirement_id"),
    [
        (SCHEDULE_ID, LARGE_ID),
        (OTHER_SCHEDULE_ID, 201),
        (47, 202),
        (48, 203),
        (49, 204),
    ],
)
def test_list_reads_requirements_in_every_schedule_status(
    shift_requirements_api, schedule_id, requirement_id
) -> None:
    app, token, _, _ = shift_requirements_api
    with TestClient(app) as client:
        response = client.get(_list_path(schedule_id), headers=_headers(token))
    assert response.status_code == 200
    assert requirement_id in [int(item["id"]) for item in response.json()["items"]]


@pytest.mark.parametrize("role", ["admin", "manager"])
def test_both_reads_allow_admin_and_manager(shift_requirements_api, role) -> None:
    app, token, actor, _ = shift_requirements_api
    actor.role.code = role
    with TestClient(app) as client:
        listing = client.get(_list_path(), headers=_headers(token))
        detail = client.get(
            _detail_path(SCHEDULE_ID, LARGE_ID), headers=_headers(token)
        )
    assert listing.status_code == detail.status_code == 200


def test_employee_is_rejected_before_resource_lookup(
    shift_requirements_api, monkeypatch
) -> None:
    app, token, actor, _ = shift_requirements_api
    actor.role.code = "employee"
    calls = 0
    original = ScheduleRepository.get_by_id

    def track_lookup(repository, schedule_id):
        nonlocal calls
        calls += 1
        return original(repository, schedule_id)

    monkeypatch.setattr(ScheduleRepository, "get_by_id", track_lookup)
    with TestClient(app) as client:
        forbidden = [
            client.get(_list_path(999), headers=_headers(token)),
            client.get(_detail_path(999, 999), headers=_headers(token)),
        ]
        unauthenticated = [client.get(_list_path()), client.get(_detail_path(45, 1))]
    assert [response.status_code for response in forbidden] == [403, 403]
    assert calls == 0
    assert [response.status_code for response in unauthenticated] == [401, 401]
    assert all(
        response.headers["content-type"] == "application/problem+json"
        for response in forbidden + unauthenticated
    )
    assert all(
        response.headers["cache-control"] == "no-store"
        for response in forbidden + unauthenticated
    )
    assert all(
        response.headers["www-authenticate"] == "Bearer" for response in unauthenticated
    )


@pytest.mark.parametrize(
    ("path", "params"),
    [
        (_list_path(), {"work_date": "2026-02-30"}),
        (_list_path(), {"work_date": "not-a-date"}),
        (_list_path(), {"work_date": "2026-11-02T00:00:00"}),
        (_list_path(), {"work_date": "1762041600"}),
        (_list_path(), {"position_id": "0"}),
        (_list_path(), {"position_id": "9223372036854775808"}),
        (_list_path(), {"limit": "0"}),
        (_list_path(), {"limit": "101"}),
        (_list_path(), {"offset": "-1"}),
        (_list_path(), {"offset": "9223372036854775808"}),
        (_list_path(), {"sort": "work_date"}),
        (_list_path(), [("limit", "1"), ("limit", "2")]),
        (f"{PATH}/invalid/shift-requirements", None),
        (f"{PATH}/-1/shift-requirements", None),
        (f"{PATH}/9223372036854775808/shift-requirements", None),
        (_detail_path(SCHEDULE_ID, -1), None),
        (_detail_path(SCHEDULE_ID, 9223372036854775808), None),
    ],
)
def test_invalid_paths_and_query_values_return_422_problem(
    shift_requirements_api, path, params
) -> None:
    app, token, _, _ = shift_requirements_api
    with TestClient(app) as client:
        response = client.get(path, params=params, headers=_headers(token))
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert response.json()["errors"]


def test_openapi_documents_read_operations_and_contract(
    shift_requirements_api,
) -> None:
    app, _, _, _ = shift_requirements_api
    schema = app.openapi()
    paths = schema["paths"]
    list_path = f"{PATH}/{{schedule_id}}/shift-requirements"
    detail_path = f"{list_path}/{{requirement_id}}"
    assert set(paths[list_path]) == {"get", "post"}
    assert set(paths[detail_path]) == {"get", "patch", "delete"}

    list_operation = paths[list_path]["get"]
    detail_operation = paths[detail_path]["get"]
    create_operation = paths[list_path]["post"]
    assert create_operation["security"] == [{"BearerAuth": []}]
    assert {"201", "400", "401", "403", "404", "409", "422"} <= set(
        create_operation["responses"]
    )
    assert create_operation["responses"]["201"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/ShiftRequirementResponse"}
    assert create_operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CreateShiftRequirementInput"
    }
    for operation, response_name in (
        (list_operation, "ShiftRequirementListResponse"),
        (detail_operation, "ShiftRequirementResponse"),
    ):
        assert operation["security"] == [{"BearerAuth": []}]
        assert {"200", "401", "403", "404", "422"} <= set(operation["responses"])
        assert operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ] == {"$ref": f"#/components/schemas/{response_name}"}
        for status in {"401", "403", "404", "422"}:
            assert (
                "application/problem+json" in operation["responses"][status]["content"]
            )

    query_names = {
        parameter["name"]
        for parameter in list_operation["parameters"]
        if parameter["in"] == "query"
    }
    assert query_names == {"work_date", "position_id", "limit", "offset"}
    assert all(
        parameter["name"] != "sort" for parameter in list_operation["parameters"]
    )
    response_fields = set(
        schema["components"]["schemas"]["ShiftRequirementResponse"]["properties"]
    )
    assert response_fields == {
        "id",
        "schedule_id",
        "position_id",
        "position_name",
        "position_is_active",
        "work_date",
        "start_time",
        "end_time",
        "required_workers",
        "note",
    }
