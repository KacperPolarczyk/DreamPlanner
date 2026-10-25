"""HTTP coverage for the transaction-scoped manager write guard."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.auth.dependencies import AuthHttpError, require_manager_write
from src.auth.session_service import ActiveIdentity
from src.users.models import Role, User
from tests.test_schedule_deletion import CREATOR_ID, PATH, _headers

pytest_plugins = ["tests.test_schedule_deletion"]

REQUIREMENT = {
    "position_id": "1",
    "work_date": "2027-01-08",
    "start_time": "09:00:00",
    "end_time": "17:00:00",
    "required_workers": 1,
}
ASSIGNMENT = {"shift_requirement_id": "20", "user_id": "43"}
WRITES = [
    ("post", PATH, {"year": 2027, "month": 2}),
    ("delete", f"{PATH}/10", None),
    ("post", f"{PATH}/10/collection-opening", None),
    ("post", f"{PATH}/10/collection-closing", None),
    ("post", f"{PATH}/10/closing", None),
    ("post", f"{PATH}/10/publication", None),
    ("post", f"{PATH}/10/shift-requirements", REQUIREMENT),
    ("patch", f"{PATH}/10/shift-requirements/20", {"note": "review"}),
    ("delete", f"{PATH}/10/shift-requirements/20", None),
    ("post", f"{PATH}/10/shift-assignments", ASSIGNMENT),
    ("delete", f"{PATH}/10/shift-assignments/30", None),
]


@pytest.mark.parametrize("method,path,body", WRITES)
def test_every_manager_write_rechecks_the_database_role(
    deletion_api, method: str, path: str, body: dict | None
) -> None:
    """Reject a stale manager identity before any write service runs."""
    app, token, actor, engine = deletion_api
    assert actor.role.code == "manager"
    with Session(engine) as session:
        session.add(Role(id=2, code="employee", name="Employee"))
        user = session.get(User, CREATOR_ID)
        user.role_id = 2
        session.commit()

    with TestClient(app) as client:
        response = client.request(method, path, json=body, headers=_headers(token))

    assert response.status_code == 403
    assert response.json() == {
        "type": "about:blank",
        "title": "Odmowa dostępu",
        "status": 403,
        "detail": "Brak uprawnienia.",
    }
    assert response.headers["cache-control"] == "no-store"
    assert "www-authenticate" not in response.headers


def test_manager_write_rechecks_account_activation(deletion_api) -> None:
    """Reject an account deactivated after the initial identity read."""
    app, token, actor, engine = deletion_api
    assert actor.is_active and actor.activated_at is not None
    with Session(engine) as session:
        user = session.get(User, CREATOR_ID)
        user.is_active = False
        session.commit()

    with TestClient(app) as client:
        response = client.post(
            PATH, json={"year": 2027, "month": 2}, headers=_headers(token)
        )

    assert response.status_code == 403
    assert "www-authenticate" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["type"] == "about:blank"


def test_guard_rejects_missing_activation_even_with_active_flag(monkeypatch) -> None:
    """The activation predicate is enforced independently of is_active."""

    class Repository:
        def __init__(self, session: object) -> None:
            assert session is marker

        def lock_users_for_share(self, ids: set[int]) -> list[tuple[object, str]]:
            assert ids == {42}
            return [(SimpleNamespace(is_active=True, activated_at=None), "manager")]

    marker = object()
    monkeypatch.setattr("src.auth.dependencies.UserRepository", Repository)
    with pytest.raises(AuthHttpError) as error:
        require_manager_write(ActiveIdentity(42, uuid4(), "manager"), marker)
    assert error.value.status == 403


def test_guard_refreshes_a_user_already_loaded_in_the_session(deletion_api) -> None:
    """The lock query must replace an ORM identity-map value read before demotion."""
    _, _, _, engine = deletion_api
    with Session(engine) as writer:
        writer.add(Role(id=2, code="employee", name="Employee"))
        writer.commit()

    with Session(engine) as request_session:
        stale = request_session.get(User, CREATOR_ID)
        assert stale.role_id == 1
        with Session(engine) as admin_session:
            current = admin_session.get(User, CREATOR_ID)
            current.role_id = 2
            admin_session.commit()

        with pytest.raises(AuthHttpError) as error:
            require_manager_write(
                ActiveIdentity(CREATOR_ID, uuid4(), "manager"), request_session
            )
        assert error.value.status == 403
        assert stale.role_id == 2
