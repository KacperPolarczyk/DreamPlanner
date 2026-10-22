"""Alembic environment using the migration account and shared ORM metadata."""

from logging.config import fileConfig

from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

from alembic import context
from src.core import models
from src.core.config import Settings

config = context.config
if config.config_file_name and config.get_section("loggers") is not None:
    fileConfig(config.config_file_name)

target_metadata = models.Base.metadata


def migration_url() -> str:
    """Return the dedicated migration URL without exposing it in Alembic logs.

    Raises:
        ValueError: If MIGRATION_DATABASE_URL is missing or uses another driver.
    """
    url = Settings().migration_database_url
    if not url:
        raise ValueError("MIGRATION_DATABASE_URL must be configured for migrations")
    if make_url(url).drivername != "postgresql+psycopg":
        raise ValueError("MIGRATION_DATABASE_URL must use postgresql+psycopg")
    return url


def run_migrations_offline() -> None:
    """Generate SQL from metadata without connecting to PostgreSQL."""
    context.configure(
        url=migration_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations through a disposable connection from the migration URL."""
    engine = create_engine(migration_url(), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
