"""HTTP and transaction checks for publishing monthly schedules."""

from datetime import UTC, date, datetime, time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from src.assignments.models import ShiftAssignment
from src.positions.models import Position
from src.schedules.models import Schedule, ShiftRequirement
from src.users.models import Role, User
from tests.test_schedule_deletion import CREATOR_ID, PATH, SCHEDULE_ID, _headers

pytest_plugins = ["tests.test_schedule_deletion"]

PUBLICATION_PATH = f"{PATH}/{SCHEDULE_ID}/publication"
NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


@pytest.fixture
def publication_api(deletion_api):
    """Start with a closed schedule and an eligible employee account."""
    app, token, actor, engine = deletion_api
    with Session(engine) as session:
        schedule = session.get(Schedule, SCHEDULE_ID)
        schedule.status = "closed"
        session.add(Role(id=2, code="employee", name="Employee"))
        session.add(
            User(
                id=43,
                role_id=2,
                email="employee@example.test",
                full_name="Employee",
                password_hash="unused",
                is_active=True,
                activated_at=NOW,
            )
        )
        session.add(
            User(
                id=45,
                role_id=1,
                email="inactive-manager@example.test",
                full_name="Inactive Manager",
                password_hash="unused",
                is_active=False,
                activated_at=NOW,
            )
        )
        session.commit()
    return app, token, actor, engine


def _add_requirement(engine, *, required_workers: int = 2, active: bool = True) -> int:
    with Session(engine) as session:
        position = session.get(Position, 1)
        position.is_active = active
        requirement = ShiftRequirement(
            id=20,
            schedule_id=SCHEDULE_ID,
            position_id=1,
            work_date=date(2027, 1, 8),
            start_time=time(9),
            end_time=time(17),
            required_workers=required_workers,
        )
        session.add(requirement)
        session.commit()
    return 20


def _add_assignment(
    engine,
    *,
    user_id: int = 43,
    assigned_by: int = CREATOR_ID,
    warning_type: str | None = None,
    acknowledged_at: datetime | None = None,
) -> None:
    with Session(engine) as session:
        session.add(
            ShiftAssignment(
                id=30,
                shift_requirement_id=20,
                user_id=user_id,
                assigned_by=assigned_by,
                assigned_at=NOW,
                warning_type=warning_type,
                warning_acknowledged_at=acknowledged_at,
            )
        )
        session.commit()


def _publish(client: TestClient, token: str, schedule_id: str = "10"):
    return client.post(f"{PATH}/{schedule_id}/publication", headers=_headers(token))


def _snapshot(engine) -> tuple:
    with Session(engine) as session:
        schedule = session.get(Schedule, SCHEDULE_ID)
        assignments = session.scalars(
            select(ShiftAssignment).order_by(ShiftAssignment.id)
        ).all()
        return (
            schedule.status,
            schedule.published_at,
            tuple(
                (
                    assignment.id,
                    assignment.user_id,
                    assignment.warning_type,
                    assignment.warning_acknowledged_at,
                )
                for assignment in assignments
            ),
        )


@pytest.mark.parametrize("warning_type", ["unavailable", "unknown"])
def test_publish_partial_staffing_with_historical_warning_and_inactive_position(
    publication_api, warning_type
) -> None:
    app, token, _, engine = publication_api
    _add_requirement(engine, required_workers=2, active=False)
    _add_assignment(
        engine,
        assigned_by=45,
        warning_type=warning_type,
        acknowledged_at=NOW,
    )

    with TestClient(app) as client:
        response = _publish(client, token)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["status"] == "published"
    assert response.json()["published_at"]
    assert set(response.json()) == {
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
    state = _snapshot(engine)
    assert state[0] == "published"
    assert state[2] == ((30, 43, warning_type, NOW.replace(tzinfo=None)),)


def test_publish_requirement_without_assignments(publication_api) -> None:
    app, token, _, engine = publication_api
    _add_requirement(engine)

    with TestClient(app) as client:
        response = _publish(client, token)

    assert response.status_code == 200
    assert _snapshot(engine)[0] == "published"


def test_publish_fully_staffed_schedule(publication_api) -> None:
    app, token, _, engine = publication_api
    _add_requirement(engine, required_workers=1)
    _add_assignment(engine)

    with TestClient(app) as client:
        response = _publish(client, token)

    assert response.status_code == 200
    assert _snapshot(engine)[0] == "published"


def test_empty_schedule_is_blocked_without_partial_change(publication_api) -> None:
    app, token, _, engine = publication_api
    before = _snapshot(engine)

    with TestClient(app) as client:
        response = _publish(client, token)

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["type"] == (
        "urn:dreamplanner:problem:schedule-publication-blocked"
    )
    assert "co najmniej jedno wymaganie" in response.json()["detail"]
    assert _snapshot(engine) == before


def test_overstaffed_requirement_is_blocked(publication_api) -> None:
    app, token, _, engine = publication_api
    _add_requirement(engine, required_workers=1)
    _add_assignment(engine, user_id=43)
    with Session(engine) as session:
        session.add(
            User(
                id=44,
                role_id=2,
                email="employee2@example.test",
                full_name="Employee 2",
                password_hash="unused",
                is_active=True,
                activated_at=NOW,
            )
        )
        session.commit()
    with Session(engine) as session:
        session.add(
            ShiftAssignment(
                id=31,
                shift_requirement_id=20,
                user_id=44,
                assigned_by=CREATOR_ID,
                assigned_at=NOW,
            )
        )
        session.commit()
    before = _snapshot(engine)

    with TestClient(app) as client:
        response = _publish(client, token)

    assert response.status_code == 409
    assert response.json()["type"] == (
        "urn:dreamplanner:problem:schedule-publication-blocked"
    )
    assert "nadmiarową obsadą" in response.json()["detail"]
    assert _snapshot(engine) == before


@pytest.mark.parametrize(
    "mutation",
    [
        lambda user: setattr(user, "is_active", False),
        lambda user: (
            setattr(user, "is_active", False),
            setattr(user, "activated_at", None),
        ),
        lambda user: setattr(user, "role_id", 1),
    ],
    ids=["inactive", "inactive-and-not-activated", "wrong-role"],
)
def test_ineligible_assignee_blocks_publication(publication_api, mutation) -> None:
    app, token, _, engine = publication_api
    _add_requirement(engine)
    _add_assignment(engine)
    with Session(engine) as session:
        mutation(session.get(User, 43))
        session.commit()
    before = _snapshot(engine)

    with TestClient(app) as client:
        response = _publish(client, token)

    assert response.status_code == 409
    assert response.json()["type"] == (
        "urn:dreamplanner:problem:schedule-publication-blocked"
    )
    assert "nieaktywnego lub nieuprawnionego" in response.json()["detail"]
    assert _snapshot(engine) == before


@pytest.mark.parametrize(
    "status", ["draft", "collecting", "collection_closed", "published"]
)
def test_only_closed_schedule_can_be_published(publication_api, status) -> None:
    app, token, _, engine = publication_api
    with Session(engine) as session:
        schedule = session.get(Schedule, SCHEDULE_ID)
        schedule.status = status
        schedule.published_at = NOW if status == "published" else None
        session.commit()
    before = _snapshot(engine)

    with TestClient(app) as client:
        response = _publish(client, token)

    assert response.status_code == 409
    assert response.json()["type"] == "urn:dreamplanner:problem:schedule-state-conflict"
    assert _snapshot(engine) == before


def test_republication_preserves_published_timestamp(publication_api) -> None:
    app, token, _, engine = publication_api
    _add_requirement(engine)
    with TestClient(app) as client:
        first = _publish(client, token)
        before = _snapshot(engine)
        second = _publish(client, token)

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["type"] == "urn:dreamplanner:problem:schedule-state-conflict"
    assert _snapshot(engine) == before


@pytest.mark.parametrize("role", ["admin", "employee"])
def test_non_manager_is_forbidden(publication_api, role) -> None:
    app, token, actor, engine = publication_api
    actor.role.code = role

    with TestClient(app) as client:
        response = _publish(client, token)

    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert _snapshot(engine)[0] == "closed"


def test_database_role_change_after_authentication_is_forbidden(
    publication_api,
) -> None:
    app, token, _, engine = publication_api
    _add_requirement(engine)
    with Session(engine) as session:
        actor = session.get(User, CREATOR_ID)
        actor.role_id = 2
        session.commit()

    with TestClient(app) as client:
        response = _publish(client, token)

    assert response.status_code == 403
    assert _snapshot(engine)[0] == "closed"


def test_inactive_manager_is_forbidden(publication_api) -> None:
    app, token, _, engine = publication_api
    _add_requirement(engine)
    with Session(engine) as session:
        actor = session.get(User, CREATOR_ID)
        actor.is_active = False
        session.commit()

    with TestClient(app) as client:
        response = _publish(client, token)

    assert response.status_code == 403
    assert _snapshot(engine)[0] == "closed"


def test_authentication_scope_and_path_validation(publication_api) -> None:
    app, token, _, engine = publication_api
    with TestClient(app) as client:
        missing_token = client.post(PUBLICATION_PATH)
        invalid_token = client.post(PUBLICATION_PATH, headers=_headers("invalid"))
        missing_schedule = _publish(client, token, "999")
        invalid_id = _publish(client, token, "9223372036854775808")
        nonempty_body = client.post(
            PUBLICATION_PATH, content=b"{}", headers=_headers(token)
        )

    assert missing_token.status_code == 401
    assert missing_token.headers["www-authenticate"] == "Bearer"
    assert invalid_token.status_code == 401
    assert missing_schedule.status_code == 404
    assert invalid_id.status_code == 422
    assert nonempty_body.status_code == 422
    assert all(
        response.headers["cache-control"] == "no-store"
        for response in (
            missing_token,
            invalid_token,
            missing_schedule,
            invalid_id,
            nonempty_body,
        )
    )
    assert _snapshot(engine)[0] == "closed"


def test_failed_update_rolls_back_publication(publication_api) -> None:
    app, token, _, engine = publication_api
    _add_requirement(engine)
    _add_assignment(engine, warning_type="unknown", acknowledged_at=NOW)
    before = _snapshot(engine)

    def fail_update(mapper, connection, schedule) -> None:
        raise RuntimeError("private_database_detail")

    event.listen(Schedule, "before_update", fail_update)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = _publish(client, token)
    finally:
        event.remove(Schedule, "before_update", fail_update)

    assert response.status_code == 500
    assert "private_database_detail" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert _snapshot(engine) == before


def test_openapi_documents_publication(publication_api) -> None:
    app, _, _, _ = publication_api
    operation = app.openapi()["paths"][f"{PATH}/{{schedule_id}}/publication"]["post"]

    assert operation["security"] == [{"BearerAuth": []}]
    assert "manager" in operation["description"]
    assert {"200", "401", "403", "404", "409", "422"} <= set(operation["responses"])
    assert "requestBody" not in operation
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ScheduleResponse"
    }
    for status in ("401", "403", "404", "409", "422"):
        assert "application/problem+json" in operation["responses"][status]["content"]
