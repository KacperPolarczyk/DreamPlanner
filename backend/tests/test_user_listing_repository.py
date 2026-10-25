"""Query behavior for filtered administrator account pages."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from src.core.database import Base
from src.users.models import Role, User
from src.users.repository import UserRepository

NOW = datetime(2027, 1, 5, 12, tzinfo=UTC)


@pytest.fixture
def users_session():
    """Create disposable account rows for read query behavior checks."""
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def register_btrim(connection, record) -> None:
        """Support the model's PostgreSQL whitespace constraint in SQLite."""
        connection.create_function("btrim", 1, str.strip)

    Base.metadata.create_all(engine, tables=[Role.__table__, User.__table__])
    with Session(engine) as session:
        manager = Role(id=1, code="manager", name="Manager")
        employee = Role(id=2, code="employee", name="Employee")
        session.add_all([manager, employee])
        session.add_all(
            [
                User(
                    id=10,
                    role_id=employee.id,
                    email="anna@example.com",
                    full_name="Anna",
                    password_hash="hidden",
                    is_active=True,
                    activated_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                User(
                    id=11,
                    role_id=employee.id,
                    email="anna2@example.com",
                    full_name="Anna",
                    password_hash="hidden",
                    is_active=False,
                    activated_at=None,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                User(
                    id=12,
                    role_id=manager.id,
                    email="beta@example.com",
                    full_name="Beta",
                    password_hash="hidden",
                    is_active=True,
                    activated_at=NOW,
                    created_at=NOW + timedelta(days=1),
                    updated_at=NOW,
                ),
                User(
                    id=13,
                    role_id=employee.id,
                    email="literal%name@example.com",
                    full_name="Gamma",
                    password_hash="hidden",
                    is_active=False,
                    activated_at=NOW,
                    created_at=NOW + timedelta(days=2),
                    updated_at=NOW,
                ),
            ]
        )
        session.commit()
        yield session
    engine.dispose()


def _list(session: Session, **overrides) -> tuple[list[User], int]:
    """Query with contract defaults overridden by a test case."""
    options = {
        "role": None,
        "is_active": None,
        "activated": None,
        "query": None,
        "limit": 25,
        "offset": 0,
        "sort": "full_name",
    }
    options.update(overrides)
    return UserRepository(session).list_users(**options)


def test_listing_includes_all_states_and_stabilizes_ties_by_id(users_session) -> None:
    """Equal sort values use ascending IDs in either sort direction."""
    ascending, total = _list(users_session)
    descending, reverse_total = _list(users_session, sort="-full_name")
    assert [user.id for user in ascending] == [10, 11, 12, 13]
    assert [user.id for user in descending] == [13, 12, 10, 11]
    assert total == reverse_total == 4


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"role": "employee"}, [10, 11, 13]),
        ({"is_active": False}, [11, 13]),
        ({"activated": False}, [11]),
        ({"activated": True, "is_active": False}, [13]),
        ({"query": "ANNA"}, [10, 11]),
        ({"query": "BETA@"}, [12]),
        ({"query": "GAMMA"}, [13]),
        ({"query": "%"}, [13]),
    ],
)
def test_listing_filters_accounts(users_session, options, expected) -> None:
    """Count and page apply the same filters, including literal search text."""
    users, total = _list(users_session, **options)
    assert [user.id for user in users] == expected
    assert total == len(expected)


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("email", [11, 10, 12, 13]),
        ("-email", [13, 12, 10, 11]),
        ("created_at", [10, 11, 12, 13]),
        ("-created_at", [13, 12, 10, 11]),
    ],
)
def test_listing_supported_sort_orders(users_session, sort, expected) -> None:
    users, total = _list(users_session, sort=sort)
    assert [user.id for user in users] == expected
    assert total == 4


def test_listing_counts_filtered_rows_before_pagination(users_session) -> None:
    users, total = _list(users_session, role="employee", limit=1, offset=1)
    empty_page, empty_total = _list(users_session, limit=1, offset=20)
    assert [user.id for user in users] == [11]
    assert total == 3
    assert empty_page == []
    assert empty_total == 4
