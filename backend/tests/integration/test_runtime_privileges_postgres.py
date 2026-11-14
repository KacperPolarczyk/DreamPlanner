"""Verify runtime CRUD and DDL isolation after real Alembic migrations."""

import os
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from alembic import command

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("TEST_DATABASE_URL")
        or not os.getenv("TEST_MIGRATION_DATABASE_URL"),
        reason="Requires disposable local PostgreSQL URLs",
    ),
]


@pytest.fixture(scope="module")
def runtime_engine():
    """Migrate only this module's isolated local database, never Azure."""
    app = make_url(os.environ["TEST_DATABASE_URL"])
    migration = make_url(os.environ["TEST_MIGRATION_DATABASE_URL"])
    assert app.host == migration.host == "127.0.0.1"
    assert app.port == migration.port
    assert (
        app.database
        == migration.database
        == "dreamplanner_test_runtime_privileges_postgres"
    )
    assert app.username == "dreamplanner_app"
    assert migration.username == "dreamplanner_migration"
    previous = os.environ.get("MIGRATION_DATABASE_URL")
    os.environ["MIGRATION_DATABASE_URL"] = os.environ["TEST_MIGRATION_DATABASE_URL"]
    try:
        command.upgrade(
            Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head"
        )
    finally:
        if previous is None:
            os.environ.pop("MIGRATION_DATABASE_URL", None)
        else:
            os.environ["MIGRATION_DATABASE_URL"] = previous
    engine = create_engine(app)
    yield engine
    engine.dispose()


def test_runtime_crud_and_identity_sequence(runtime_engine):
    """App role can use a real migrated table and its generated identity."""
    with runtime_engine.connect() as connection:
        transaction = connection.begin()
        try:
            key = connection.scalar(
                text(
                    "INSERT INTO positions(name) "
                    "VALUES ('DBA CI position') RETURNING id"
                )
            )
            assert isinstance(key, int)
            assert (
                connection.scalar(
                    text("SELECT name FROM positions WHERE id=:id"), {"id": key}
                )
                == "DBA CI position"
            )
            connection.execute(
                text("UPDATE positions SET name='DBA CI updated' WHERE id=:id"),
                {"id": key},
            )
            assert (
                connection.scalar(
                    text("SELECT name FROM positions WHERE id=:id"), {"id": key}
                )
                == "DBA CI updated"
            )
            connection.execute(text("DELETE FROM positions WHERE id=:id"), {"id": key})
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM positions WHERE id=:id"), {"id": key}
                )
                == 0
            )
        finally:
            transaction.rollback()


def test_runtime_grants_on_every_migrated_table(runtime_engine):
    """Each DML permission is tested separately; comma lists mean ANY in PG."""
    with runtime_engine.connect() as connection:
        missing = connection.execute(
            text("""
            SELECT tablename, privilege
            FROM pg_tables
            CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE']) privilege
            WHERE schemaname='public' AND tablename <> 'alembic_version'
            AND NOT has_table_privilege(
                current_user,
                quote_ident(schemaname)||'.'||quote_ident(tablename), privilege
            )
        """)
        ).all()
        assert missing == []
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM pg_tables WHERE schemaname='public' "
                    "AND tablename <> 'alembic_version'"
                )
            )
            == 11
        )
        flags = connection.execute(
            text(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
                "rolbypassrls FROM pg_roles WHERE rolname=current_user"
            )
        ).one()
        assert not any(flags)


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE public.forbidden_runtime_table(id integer)",
        "ALTER TABLE public.positions ADD COLUMN forbidden_column integer",
        "DROP TABLE public.roles CASCADE",
        "CREATE SCHEMA forbidden_runtime_schema",
        "CREATE ROLE forbidden_runtime_role",
        "SET ROLE dreamplanner_migration",
    ],
)
def test_runtime_cannot_change_schema_or_escalate(runtime_engine, statement):
    """SQL must reject runtime DDL and role escalation with privilege error."""
    with runtime_engine.connect() as connection:
        with pytest.raises(DBAPIError) as error:
            connection.execute(text(statement))
        assert error.value.orig.sqlstate == "42501"
        connection.rollback()
