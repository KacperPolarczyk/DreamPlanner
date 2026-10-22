"""Opt-in checks for the local PostgreSQL accounts and SQLAlchemy connections."""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from src.core.config import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_DB_INTEGRATION") != "1",
        reason="Set RUN_DB_INTEGRATION=1 to test local PostgreSQL",
    ),
]


@pytest.mark.parametrize(
    ("setting_name", "expected_user", "can_create"),
    [
        ("database_url", "dreamplanner_app", False),
        ("migration_database_url", "dreamplanner_migration", True),
    ],
)
def test_local_postgres_connection_and_schema_privileges(
    setting_name: str, expected_user: str, can_create: bool
) -> None:
    """Connect through each configured role and check its schema permission."""
    url = getattr(Settings(), setting_name)
    assert url is not None, f"{setting_name.upper()} must be configured"
    assert make_url(url).host in {"localhost", "127.0.0.1"}, (
        "Integration tests may connect only to local PostgreSQL"
    )

    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT 1 AS value, current_user AS role_name, "
                    "has_schema_privilege(current_user, 'public', 'CREATE') "
                    "AS can_create"
                )
            ).one()
            assert row.value == 1
            assert row.role_name == expected_user
            assert row.can_create is can_create
    finally:
        engine.dispose()
