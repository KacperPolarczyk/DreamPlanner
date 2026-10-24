"""SQLite HTTP checks for opening monthly availability collection."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.schedules.models import Schedule
from tests.test_schedule_deletion import CREATOR_ID, PATH, SCHEDULE_ID, _headers

pytest_plugins = ["tests.test_schedule_deletion"]

OPEN_PATH = f"{PATH}/{SCHEDULE_ID}/collection-opening"


def _open(client: TestClient, token: str, schedule_id: str = "10"):
    return client.post(
        f"{PATH}/{schedule_id}/collection-opening", headers=_headers(token)
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


def test_draft_opens_collection_and_get_returns_committed_state(deletion_api) -> None:
    app, token, _, engine = deletion_api
    original = _snapshot(engine)
    before = datetime.now(UTC) - timedelta(seconds=2)
    with TestClient(app) as client:
        opened = _open(client, token)
        detail = client.get(f"{PATH}/{SCHEDULE_ID}", headers=_headers(token))
    after = datetime.now(UTC) + timedelta(seconds=2)
    assert opened.status_code == 200
    assert opened.headers["cache-control"] == "no-store"
    assert opened.headers["content-type"] == "application/json"
    payload = opened.json()
    assert (
        payload
        == detail.json()
        == {
            "id": str(SCHEDULE_ID),
            "year": 2027,
            "month": 1,
            "status": "collecting",
            "created_by": str(CREATOR_ID),
            "created_at": detail.json()["created_at"],
            "collection_opened_at": detail.json()["collection_opened_at"],
            "collection_closed_at": None,
            "published_at": None,
        }
    )
    opened_at = datetime.fromisoformat(payload["collection_opened_at"])
    assert opened_at.tzinfo is not None
    assert opened_at.utcoffset() == timedelta(0)
    assert before <= opened_at <= after
    assert payload["created_at"] == original[2].replace(tzinfo=UTC).isoformat().replace(
        "+00:00", "Z"
    )
    persisted = _snapshot(engine)
    assert persisted[0] == "collecting"
    assert persisted[1:3] == original[1:3]
    assert persisted[3] is not None
    assert persisted[4:] == original[4:]


def test_other_timestamps_remain_unchanged(deletion_api) -> None:
    app, token, _, engine = deletion_api
    closed_at = datetime(2025, 1, 1, 10, tzinfo=UTC)
    published_at = datetime(2025, 1, 2, 10, tzinfo=UTC)
    with Session(engine) as session:
        row = session.get(Schedule, SCHEDULE_ID)
        row.collection_closed_at = closed_at
        row.published_at = published_at
        session.commit()
    before = _snapshot(engine)
    with TestClient(app) as client:
        response = _open(client, token)
    after = _snapshot(engine)
    assert response.status_code == 200
    assert after[1:3] == before[1:3]
    assert after[4:] == before[4:]
    assert datetime.fromisoformat(response.json()["collection_closed_at"]) == closed_at
    assert datetime.fromisoformat(response.json()["published_at"]) == published_at


@pytest.mark.parametrize(
    "status", ["collecting", "collection_closed", "closed", "published"]
)
def test_other_states_return_409_without_changes(deletion_api, status) -> None:
    app, token, _, engine = deletion_api
    with Session(engine) as session:
        row = session.get(Schedule, SCHEDULE_ID)
        row.status = status
        row.collection_opened_at = datetime(2026, 10, 24, 10, tzinfo=UTC)
        row.collection_closed_at = datetime(2026, 10, 24, 11, tzinfo=UTC)
        row.published_at = datetime(2026, 10, 24, 12, tzinfo=UTC)
        session.commit()
    before = _snapshot(engine)
    with TestClient(app) as client:
        response = _open(client, token)
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": "urn:dreamplanner:problem:schedule-state-conflict",
        "title": "Niedozwolona zmiana stanu grafiku",
        "status": 409,
        "detail": "Zbieranie dyspozycyjności można otworzyć tylko dla grafiku draft.",
    }
    assert _snapshot(engine) == before


def test_repeat_returns_409_without_overwriting_opening_time(deletion_api) -> None:
    app, token, _, engine = deletion_api
    with TestClient(app) as client:
        first = _open(client, token)
        before = _snapshot(engine)
        repeated = _open(client, token)
    assert first.status_code == 200
    assert repeated.status_code == 409
    assert repeated.json()["type"] == "urn:dreamplanner:problem:schedule-state-conflict"
    assert _snapshot(engine) == before


@pytest.mark.parametrize("role", ["admin", "employee"])
def test_other_roles_are_forbidden_before_lookup(deletion_api, role) -> None:
    app, token, actor, engine = deletion_api
    actor.role.code = role
    with TestClient(app) as client:
        responses = [_open(client, token, schedule_id) for schedule_id in ("10", "999")]
    assert [r.status_code for r in responses] == [403, 403]
    assert all(r.headers["cache-control"] == "no-store" for r in responses)
    assert _snapshot(engine)[0] == "draft"


def test_no_token_is_401(deletion_api) -> None:
    app, _, _, engine = deletion_api
    with TestClient(app) as client:
        response = client.post(OPEN_PATH)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"
    assert _snapshot(engine)[0] == "draft"


@pytest.mark.parametrize("schedule_id", ["0", "999"])
def test_missing_schedule_is_404(deletion_api, schedule_id) -> None:
    app, token, _, engine = deletion_api
    with TestClient(app) as client:
        response = _open(client, token, schedule_id)
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["type"] == "about:blank"
    assert _snapshot(engine)[0] == "draft"


@pytest.mark.parametrize(
    "schedule_id", ["invalid", "-1", "１２", "9223372036854775808"]
)
def test_invalid_schedule_id_is_422(deletion_api, schedule_id) -> None:
    app, token, _, engine = deletion_api
    with TestClient(app) as client:
        response = _open(client, token, schedule_id)
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert response.json()["errors"]
    assert _snapshot(engine)[0] == "draft"


@pytest.mark.parametrize("content", [b"{}", b"null", b" ", b"text"])
def test_nonempty_body_is_422(deletion_api, content) -> None:
    app, token, _, engine = deletion_api
    with TestClient(app) as client:
        response = client.post(OPEN_PATH, content=content, headers=_headers(token))
    assert response.status_code == 422
    assert response.json()["type"] == "urn:dreamplanner:problem:validation-error"
    assert _snapshot(engine)[0] == "draft"


def test_openapi_documents_collection_opening(deletion_api) -> None:
    app, _, _, _ = deletion_api
    operation = app.openapi()["paths"][f"{PATH}/{{schedule_id}}/collection-opening"][
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
