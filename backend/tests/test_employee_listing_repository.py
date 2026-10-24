"""SQL filtering, search, counting, and pagination for eligible employees."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from src.core.database import Base
from src.users.models import Role, User
from src.users.repository import UserRepository

NOW = datetime(2027, 1, 5, 12, tzinfo=UTC)
BIG_ID = 9007199254740993


@pytest.fixture
def users_session():
    """Use a disposable database with all relevant roles and account states."""
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def register_btrim(connection, record) -> None:
        connection.create_function("btrim", 1, str.strip)

    Base.metadata.create_all(engine, tables=[Role.__table__, User.__table__])
    with Session(engine) as session:
        session.add_all(
            [
                Role(id=1, code="admin", name="Admin"),
                Role(id=2, code="manager", name="Manager"),
                Role(id=3, code="employee", name="Employee"),
            ]
        )
        entries = [
            (10, 3, "anna@example.com", "Anna", True, NOW),
            (11, 3, "second@example.com", "Anna", True, NOW),
            (12, 3, "hidden@example.com", "Hidden", False, NOW),
            (13, 3, "pending@example.com", "Pending", False, None),
            (14, 2, "manager@example.com", "Manager", True, NOW),
            (15, 1, "admin@example.com", "Admin", True, NOW),
            (16, 3, "reactivated@example.com", "Reactivated", True, NOW),
            (17, 3, "literal-percent@example.com", "Percent%Name", True, NOW),
            (18, 3, "literal-underscore@example.com", "Under_score", True, NOW),
            (19, 3, "literal-slash@example.com", "Back\\slash", True, NOW),
            (BIG_ID, 3, "large@example.com", "Zed", True, NOW),
        ]
        session.add_all(
            User(
                id=user_id,
                role_id=role_id,
                email=email,
                full_name=name,
                password_hash="hidden",
                is_active=active,
                activated_at=activated_at,
                created_at=NOW,
                updated_at=NOW,
            )
            for user_id, role_id, email, name, active, activated_at in entries
        )
        session.commit()
        yield session
    engine.dispose()


def _list(session: Session, query=None, limit=25, offset=0):
    return UserRepository(session).list_eligible_employees(
        query=query, limit=limit, offset=offset
    )


def test_only_eligible_employees_and_stable_order(users_session) -> None:
    rows, total = _list(users_session)
    assert [user_id for user_id, _ in rows] == [
        10,
        11,
        19,
        17,
        16,
        18,
        BIG_ID,
    ]
    assert total == 7
    assert all(isinstance(user_id, int) for user_id, _ in rows)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("ANN", [10, 11]),
        ("nNa", [10, 11]),
        ("large@", []),
        ("%", [17]),
        ("_", [18]),
        ("\\", [19]),
        ("", [10, 11, 19, 17, 16, 18, BIG_ID]),
    ],
)
def test_name_only_literal_search_and_filtered_total(
    users_session, query, expected
) -> None:
    rows, total = _list(users_session, query=query)
    assert [user_id for user_id, _ in rows] == expected
    assert total == len(expected)


def test_count_uses_same_filter_before_sql_pagination(users_session) -> None:
    page, total = _list(users_session, query="Anna", limit=1, offset=1)
    empty, empty_total = _list(users_session, query="Anna", limit=1, offset=20)
    max_offset, max_total = _list(users_session, offset=9223372036854775807)
    assert page == [(11, "Anna")]
    assert total == empty_total == 2
    assert empty == max_offset == []
    assert max_total == 7
