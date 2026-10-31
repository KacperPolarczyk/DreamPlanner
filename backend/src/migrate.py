"""Run serialized Azure migrations without disclosing database credentials."""

import sys
import time
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, pool, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError

from alembic import command
from src.core.config import Settings
from src.core.database_url import normalize_postgresql_url

MIGRATION_LOCK = 770760006
EXPECTED_HOST = "psql-dreamplanner-dev-77076.postgres.database.azure.com"


def validated_url(value: str | None) -> URL:
    """Require the dedicated Azure migration account and verified TLS."""
    if not value:
        raise ValueError("MIGRATION_DATABASE_URL must be configured")
    url = normalize_postgresql_url(value)
    if (
        url.host != EXPECTED_HOST
        or url.database != "dreamplanner"
        or url.username != "dreamplanner_migrator"
        or url.port not in (None, 5432)
        or not url.password
        or url.query.get("sslmode") != "verify-full"
        or url.query.get("sslrootcert") != "/etc/ssl/certs/ca-certificates.crt"
        or set(url.query) - {"sslmode", "sslrootcert"}
    ):
        raise ValueError("Migration target, account or TLS configuration is invalid")
    return url


def migrate() -> None:
    """Wait for firewall propagation, lock, upgrade once, and verify the head."""
    url = validated_url(Settings().migration_database_url)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise ValueError("Exactly one Alembic head is required")
    engine = create_engine(
        url,
        poolclass=pool.NullPool,
        hide_parameters=True,
        connect_args={"connect_timeout": 10},
    )
    connection = None
    try:
        # Only connection establishment is retried. Never retry a failed DDL upgrade.
        for attempt in range(30):
            try:
                connection = engine.connect()
                break
            except OperationalError:
                if attempt == 29:
                    raise
                time.sleep(10)
        assert connection is not None
        with connection:
            locked = connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": MIGRATION_LOCK}
            )
            connection.commit()
            if not locked:
                raise RuntimeError("Another automated migration is running")
            try:
                before = MigrationContext.configure(connection).get_current_heads()
                connection.commit()
                print(
                    f"Database revision before upgrade: {','.join(before) or 'empty'}"
                )
                # Keep this lock session open while Alembic uses its own connection.
                command.upgrade(config, "head")
                after = MigrationContext.configure(connection).get_current_heads()
                if set(after) != set(heads):
                    raise RuntimeError("Database did not reach the expected revision")
                print(f"Database revision verified: {','.join(after)}")
            finally:
                connection.rollback()
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK}
                )
                connection.commit()
    finally:
        engine.dispose()


def main() -> int:
    """Return a failing exit code without logging connection strings or SQL."""
    try:
        migrate()
    except Exception as error:
        code = getattr(getattr(error, "orig", None), "sqlstate", None)
        print(
            f"Migration failed: {type(error).__name__}; SQLSTATE={code}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
