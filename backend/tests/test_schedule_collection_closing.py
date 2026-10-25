"""SQLite HTTP checks for closing monthly availability collection."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from src.schedules.models import Schedule
from tests.test_schedule_deletion import CREATOR_ID, PATH, SCHEDULE_ID, _headers

pytest_plugins = ["tests.test_schedule_deletion"]

CLOSE_PATH = f"{PATH}/{SCHEDULE_ID}/collection-closing"
OPENED_AT = datetime(2026, 10, 24, 10, tzinfo=UTC)


@pytest.fixture
def collecting_api(deletion_api):
    """Start with a collecting schedule and its already recorded opening time."""
    app, token, actor, engine = deletion_api
    with Session(engine) as session:
        schedule = session.get(Schedule, SCHEDULE_ID)
        schedule.status = "collecting"
        schedule.collection_opened_at = OPENED_AT
        session.commit()
    return app, token, actor, engine


def _close(client: TestClient, token: str, schedule_id: str = "10"):
    return client.post(
        f"{PATH}/{schedule_id}/collection-closing", headers=_headers(token)
    )


def _snapshot(engine) -> tuple:
    with Session(engine) as session:
        row = session.get(Schedule, SCHEDULE_ID)
        assert row is not None
        return (
            row.status,
            row.created_by,
            row.created_at,
            row.collection_opened_at,
            row.collection_closed_at,
            row.published_at,
        )


def test_collecting_closes_and_get_returns_committed_state(collecting_api) -> None:
    app, token, _, engine = collecting_api
    original = _snapshot(engine)
    before = datetime.now(UTC) - timedelta(seconds=2)
    with TestClient(app) as client:
        closed = _close(client, token)
        detail = client.get(f"{PATH}/{SCHEDULE_ID}", headers=_headers(token))
    after = datetime.now(UTC) + timedelta(seconds=2)
    assert closed.status_code == 200
    assert closed.headers["cache-control"] == "no-store"
    assert closed.headers["content-type"] == "application/json"
    payload = closed.json()
    assert (
        payload
        == detail.json()
        == {
            "id": str(SCHEDULE_ID),
            "year": 2027,
            "month": 1,
            "status": "collection_closed",
            "created_by": str(CREATOR_ID),
            "created_at": detail.json()["created_at"],
            "collection_opened_at": "2026-10-24T10:00:00Z",
            "collection_closed_at": detail.json()["collection_closed_at"],
            "published_at": None,
        }
    )
    closed_at = datetime.fromisoformat(payload["collection_closed_at"])
    assert closed_at.tzinfo is not None
    assert closed_at.utcoffset() == timedelta(0)
    assert before <= closed_at <= after
    assert payload["created_at"] == original[2].replace(tzinfo=UTC).isoformat().replace(
        "+00:00", "Z"
    )
    persisted = _snapshot(engine)
    assert persisted[0] == "collection_closed"
    assert persisted[1:4] == original[1:4]
    assert persisted[4] is not None
    assert persisted[5] == original[5]


def test_published_timestamp_remains_unchanged(collecting_api) -> None:
    app, token, _, engine = collecting_api
    published_at = datetime(2025, 1, 2, 10, tzinfo=UTC)
    with Session(engine) as session:
        row = session.get(Schedule, SCHEDULE_ID)
        row.published_at = published_at
        session.commit()
    before = _snapshot(engine)
    with TestClient(app) as client:
        response = _close(client, token)
    after = _snapshot(engine)
    assert response.status_code == 200
    assert after[1:4] == before[1:4]
    assert after[5] == before[5]
    assert datetime.fromisoformat(response.json()["published_at"]) == published_at


@pytest.mark.parametrize(
    "status", ["draft", "collection_closed", "closed", "published"]
)
def test_other_states_return_409_without_changes(collecting_api, status) -> None:
    app, token, _, engine = collecting_api
    with Session(engine) as session:
        row = session.get(Schedule, SCHEDULE_ID)
        row.status = status
        row.collection_closed_at = datetime(2026, 10, 24, 11, tzinfo=UTC)
        row.published_at = datetime(2026, 10, 24, 12, tzinfo=UTC)
        session.commit()
    before = _snapshot(engine)
    with TestClient(app) as client:
        response = _close(client, token)
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": "urn:dreamplanner:problem:schedule-state-conflict",
        "title": "Niedozwolona zmiana stanu grafiku",
        "status": 409,
        "detail": (
            "Zbieranie dyspozycyjności można zamknąć tylko dla grafiku collecting."
        ),
    }
    assert _snapshot(engine) == before


def test_repeat_returns_409_without_overwriting_closing_time(collecting_api) -> None:
    app, token, _, engine = collecting_api
    with TestClient(app) as client:
        first = _close(client, token)
        before = _snapshot(engine)
        repeated = _close(client, token)
    assert first.status_code == 200
    assert repeated.status_code == 409
    assert repeated.json()["type"] == "urn:dreamplanner:problem:schedule-state-conflict"
    assert _snapshot(engine) == before


def test_failed_write_rolls_back_and_can_be_retried(collecting_api) -> None:
    app, token, _, engine = collecting_api
    before = _snapshot(engine)

    def fail_update(mapper, connection, schedule) -> None:
        raise RuntimeError("private_database_detail")

    event.listen(Schedule, "before_update", fail_update)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            failure = _close(client, token)
    finally:
        event.remove(Schedule, "before_update", fail_update)
    assert failure.status_code == 500
    assert failure.headers["content-type"] == "application/problem+json"
    assert "private_database_detail" not in failure.text
    assert _snapshot(engine) == before
    with TestClient(app) as client:
        retry = _close(client, token)
    assert retry.status_code == 200


@pytest.mark.parametrize("role", ["admin", "employee"])
def test_other_roles_are_forbidden_before_lookup(collecting_api, role) -> None:
    app, token, actor, engine = collecting_api
    actor.role.code = role
    with TestClient(app) as client:
        responses = [
            _close(client, token, schedule_id) for schedule_id in ("10", "999")
        ]
    assert [r.status_code for r in responses] == [403, 403]
    assert all(r.headers["cache-control"] == "no-store" for r in responses)
    assert _snapshot(engine)[0] == "collecting"


def test_no_token_is_401(collecting_api) -> None:
    app, _, _, engine = collecting_api
    with TestClient(app) as client:
        response = client.post(CLOSE_PATH)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"
    assert _snapshot(engine)[0] == "collecting"


@pytest.mark.parametrize("schedule_id", ["0", "999"])
def test_missing_schedule_is_404(collecting_api, schedule_id) -> None:
    app, token, _, engine = collecting_api
    with TestClient(app) as client:
        response = _close(client, token, schedule_id)
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["type"] == "about:blank"
    assert _snapshot(engine)[0] == "collecting"


@pytest.mark.parametrize(
    "schedule_id", ["invalid", "-1", "１２", "9223372036854775808"]
)
def test_invalid_schedule_id_is_422(collecting_api, schedule_id) -> None:
    app, token, _, engine = collecting_api
    with TestClient(app) as client:
        response = _close(client, token, schedule_id)
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert response.json()["errors"]
    assert _snapshot(engine)[0] == "collecting"


@pytest.mark.parametrize("content", [b"{}", b"null", b" ", b"text"])
def test_nonempty_body_is_422(collecting_api, content) -> None:
    app, token, _, engine = collecting_api
    with TestClient(app) as client:
        response = client.post(CLOSE_PATH, content=content, headers=_headers(token))
    assert response.status_code == 422
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert _snapshot(engine)[0] == "collecting"


def test_openapi_documents_collection_closing(collecting_api) -> None:
    app, _, _, _ = collecting_api
    operation = app.openapi()["paths"][f"{PATH}/{{schedule_id}}/collection-closing"][
        "post"
    ]
    assert operation["security"] == [{"BearerAuth": []}]
    assert "`manager`" in operation["description"]
    assert {"200", "401", "403", "404", "409", "422"} <= set(operation["responses"])
    assert "requestBody" not in operation
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ScheduleResponse"
    }
    for status in ("401", "403", "404", "409", "422"):
        assert "application/problem+json" in operation["responses"][status]["content"]
