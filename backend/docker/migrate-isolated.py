"""Run an explicit Alembic upgrade only against an isolated local database."""

import argparse
import os
from pathlib import Path

from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from alembic import command

DATABASES = {
    "app": (
        "MIGRATION_DATABASE_URL",
        "dreamplanner_stage1632b",
        "dreamplanner_isolated_migration",
    ),
    "test": (
        "TEST_MIGRATION_DATABASE_URL",
        "dreamplanner_test_publication_races_134",
        "dreamplanner_migration",
    ),
}


def main() -> None:
    """Validate the target identity before allowing Alembic to connect."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", choices=DATABASES)
    args = parser.parse_args()

    variable, database_name, role = DATABASES[args.database]
    value = os.environ.get(variable)
    if not value:
        parser.error(f"{variable} must be configured")
    try:
        url = make_url(value)
    except (ArgumentError, ValueError):
        parser.error("Migration URL is invalid")
    port = int(os.environ.get("ISOLATED_POSTGRES_HOST_PORT", "55432"))
    if (
        url.drivername != "postgresql+psycopg"
        or url.host != "127.0.0.1"
        or url.port != port
        or url.database != database_name
        or url.username != role
    ):
        parser.error("Migration URL does not identify the isolated local database")

    os.environ["MIGRATION_DATABASE_URL"] = value
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")


if __name__ == "__main__":
    main()
