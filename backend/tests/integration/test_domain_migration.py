"""Exercise the initial migration on an explicit disposable PostgreSQL database."""

import os
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import DBAPIError

from alembic import command
from src.core.config import Settings
from src.core.models import Base

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("TEST_MIGRATION_DATABASE_URL")
        or not os.getenv("TEST_DATABASE_URL"),
        reason="Set both TEST_*_DATABASE_URL values for a disposable local database",
    ),
]

BACKEND_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TABLES = set(Base.metadata.tables)
EXPLICIT_INDEXES = {
    "ux_users_email_lower",
    "ix_shift_requirements_schedule_date",
    "ix_availability_submissions_user",
    "ix_availability_entries_submission_date",
    "ix_shift_assignments_user",
}
EXPECTED_FUNCTIONS = {
    "validate_schedule_dates",
    "prevent_overlapping_availability",
    "validate_shift_assignment",
}
EXPECTED_TRIGGERS = {
    "trg_shift_period",
    "trg_availability_period",
    "trg_availability_overlap",
    "trg_assignment_overlap",
}


def _check_disposable_urls(migration_url: str, app_url: str) -> None:
    """Reject a development, remote, or mismatched database before any DDL."""
    migration = make_url(migration_url)
    app = make_url(app_url)
    assert migration.drivername == app.drivername == "postgresql+psycopg"
    assert migration.host in {"localhost", "127.0.0.1"}
    assert app.host in {"localhost", "127.0.0.1"}
    assert (migration.host, migration.port) == (app.host, app.port)
    assert migration.database == app.database
    assert migration.database is not None
    assert migration.database.startswith("dreamplanner_test_")
    assert migration.username == "dreamplanner_migration"
    assert app.username == "dreamplanner_app"
    assert migration.database not in {
        make_url(url).database
        for url in (Settings().database_url, Settings().migration_database_url)
        if url
    }


def _reject_existing_schema(connection: Connection) -> None:
    """Require a fresh disposable database rather than clearing existing data."""
    relations = connection.execute(
        text(
            "SELECT relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','S','f')"
        )
    ).scalars()
    assert list(relations) == []


def _expect_rejection(connection: Connection, statement: str, **values: object) -> None:
    """Roll back one expected PostgreSQL error without losing other test rows."""
    with pytest.raises(DBAPIError), connection.begin_nested():
        connection.execute(text(statement), values)


def _check_catalog(connection: Connection) -> None:
    """Compare migrated objects and columns with domain ORM metadata."""
    inspector = inspect(connection)
    assert set(inspector.get_table_names()) == EXPECTED_TABLES | {"alembic_version"}
    dialect = postgresql.dialect()
    for name, model_table in Base.metadata.tables.items():
        reflected = {column["name"]: column for column in inspector.get_columns(name)}
        assert set(reflected) == set(model_table.c.keys())
        for model_column in model_table.c:
            actual = reflected[model_column.name]
            assert actual["type"].compile(dialect=dialect) == model_column.type.compile(
                dialect=dialect
            )
            assert actual["nullable"] == model_column.nullable
            assert bool(actual.get("identity")) == (model_column.identity is not None)
            assert bool(actual.get("default")) == (
                model_column.server_default is not None
                and model_column.identity is None
            )

    constraints = dict(
        connection.execute(
            text(
                "SELECT contype, count(*) FROM pg_constraint c "
                "JOIN pg_namespace n ON n.oid = c.connamespace "
                "WHERE n.nspname = 'public' AND c.conrelid <> 0 "
                "GROUP BY contype"
            )
        ).all()
    )
    assert constraints == {"p": 9, "f": 10, "u": 7, "c": 12}

    indexes = dict(
        connection.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' "
                "AND indexname = ANY(:names)"
            ),
            {"names": sorted(EXPLICIT_INDEXES)},
        ).all()
    )
    assert set(indexes) == EXPLICIT_INDEXES
    assert "lower((email)::text)" in indexes["ux_users_email_lower"]
    assert "CREATE UNIQUE INDEX" in indexes["ux_users_email_lower"]

    functions = set(
        connection.execute(
            text(
                "SELECT p.proname FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public'"
            )
        ).scalars()
    )
    assert functions == EXPECTED_FUNCTIONS
    triggers = dict(
        connection.execute(
            text(
                "SELECT t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND NOT t.tgisinternal"
            )
        ).all()
    )
    assert set(triggers) == EXPECTED_TRIGGERS
    assert "UPDATE OF schedule_id, work_date" in triggers["trg_shift_period"]
    assert "UPDATE OF submission_id, work_date" in triggers["trg_availability_period"]
    assert "BEFORE INSERT OR UPDATE ON" in triggers["trg_availability_overlap"]
    assert (
        "UPDATE OF shift_requirement_id, user_id" in triggers["trg_assignment_overlap"]
    )

    assert connection.execute(
        text("SELECT id, code, name FROM roles ORDER BY id")
    ).all() == [
        (1, "admin", "Administrator"),
        (2, "manager", "Menadżer"),
        (3, "employee", "Pracownik"),
    ]


def _check_database_rules(connection: Connection) -> None:
    """Exercise reference constraints and each of the four triggers."""
    transaction = connection.begin()
    try:
        employee_id = connection.execute(
            text(
                "INSERT INTO users(role_id,email,full_name,password_hash) "
                "VALUES (3,'worker@example.test','Worker','test-hash') RETURNING id"
            )
        ).scalar_one()
        manager_id = connection.execute(
            text(
                "INSERT INTO users(role_id,email,full_name,password_hash) "
                "VALUES (2,'manager@example.test','Manager','test-hash') RETURNING id"
            )
        ).scalar_one()
        position_id = connection.execute(
            text("INSERT INTO positions(name) VALUES ('Test position') RETURNING id")
        ).scalar_one()
        schedule_id = connection.execute(
            text(
                "INSERT INTO schedules(year,month,created_by) "
                "VALUES (2027,1,:manager) RETURNING id"
            ),
            {"manager": manager_id},
        ).scalar_one()
        shift_id = connection.execute(
            text(
                "INSERT INTO shift_requirements"
                "(schedule_id,position_id,work_date,start_time,end_time,"
                "required_workers) VALUES (:schedule,:position,'2027-01-05',"
                "'08:00','12:00',2) RETURNING id"
            ),
            {"schedule": schedule_id, "position": position_id},
        ).scalar_one()
        overlapping_shift_id = connection.execute(
            text(
                "INSERT INTO shift_requirements"
                "(schedule_id,position_id,work_date,start_time,end_time,"
                "required_workers) VALUES (:schedule,:position,'2027-01-05',"
                "'11:00','15:00',2) RETURNING id"
            ),
            {"schedule": schedule_id, "position": position_id},
        ).scalar_one()
        submission_id = connection.execute(
            text(
                "INSERT INTO availability_submissions(schedule_id,user_id) "
                "VALUES (:schedule,:user) RETURNING id"
            ),
            {"schedule": schedule_id, "user": employee_id},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO availability_entries"
                "(submission_id,work_date,start_time,end_time,status) "
                "VALUES (:submission,'2027-01-05','08:00','10:00','available'),"
                "(:submission,'2027-01-05','10:00','12:00','unavailable')"
            ),
            {"submission": submission_id},
        )
        connection.execute(
            text(
                "INSERT INTO shift_assignments"
                "(shift_requirement_id,user_id,assigned_by) "
                "VALUES (:shift,:user,:manager)"
            ),
            {"shift": shift_id, "user": employee_id, "manager": manager_id},
        )

        _expect_rejection(
            connection,
            "INSERT INTO users(role_id,email,full_name,password_hash) "
            "VALUES (3,'WORKER@example.test','Duplicate','test-hash')",
        )
        _expect_rejection(
            connection,
            "INSERT INTO users(role_id,email,full_name,password_hash) "
            "VALUES (3,'  ','Blank','test-hash')",
        )
        _expect_rejection(
            connection,
            "INSERT INTO users(role_id,email,full_name,password_hash) "
            "VALUES (99,'wrong-role@example.test','Wrong role','test-hash')",
        )
        _expect_rejection(
            connection,
            "INSERT INTO schedules(year,month,created_by) VALUES (2027,1,:manager)",
            manager=manager_id,
        )
        _expect_rejection(
            connection,
            "INSERT INTO schedules(year,month,created_by) VALUES (2027,13,:manager)",
            manager=manager_id,
        )
        _expect_rejection(
            connection,
            "INSERT INTO shift_requirements"
            "(schedule_id,position_id,work_date,start_time,end_time,required_workers) "
            "VALUES (:schedule,:position,'2027-02-05','08:00','12:00',2)",
            schedule=schedule_id,
            position=position_id,
        )
        _expect_rejection(
            connection,
            "INSERT INTO shift_requirements"
            "(schedule_id,position_id,work_date,start_time,end_time,required_workers) "
            "VALUES (:schedule,:position,'2027-01-06','08:00','12:00',0)",
            schedule=schedule_id,
            position=position_id,
        )
        _expect_rejection(
            connection,
            "INSERT INTO shift_requirements"
            "(schedule_id,position_id,work_date,start_time,end_time,required_workers) "
            "VALUES (:schedule,:position,'2027-01-06','12:00','08:00',2)",
            schedule=schedule_id,
            position=position_id,
        )
        _expect_rejection(
            connection,
            "INSERT INTO availability_entries"
            "(submission_id,work_date,start_time,end_time,status) "
            "VALUES (:submission,'2027-02-05','08:00','10:00','available')",
            submission=submission_id,
        )
        _expect_rejection(
            connection,
            "INSERT INTO availability_entries"
            "(submission_id,work_date,start_time,end_time,status) "
            "VALUES (:submission,'2027-01-05','09:00','11:00','available')",
            submission=submission_id,
        )
        _expect_rejection(
            connection,
            "INSERT INTO availability_entries"
            "(submission_id,work_date,start_time,end_time,status) "
            "VALUES (:submission,'2027-01-06','08:00','10:00','unknown')",
            submission=submission_id,
        )
        _expect_rejection(
            connection,
            "INSERT INTO shift_assignments(shift_requirement_id,user_id,assigned_by) "
            "VALUES (:shift,:user,:manager)",
            shift=overlapping_shift_id,
            user=employee_id,
            manager=manager_id,
        )
        _expect_rejection(
            connection,
            "INSERT INTO shift_assignments"
            "(shift_requirement_id,user_id,assigned_by,warning_type) "
            "VALUES (:shift,:manager,:manager,'unknown')",
            shift=shift_id,
            manager=manager_id,
        )
    finally:
        transaction.rollback()


def _check_app_privileges(app_url: str) -> None:
    """Use the application account to read and write with identity sequences."""
    engine = create_engine(app_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_user")) == "dreamplanner_app"
            for table in sorted(EXPECTED_TABLES):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert connection.scalar(
                        text(
                            "SELECT has_table_privilege("
                            "current_user, :table, :privilege)"
                        ),
                        {"table": f"public.{table}", "privilege": privilege},
                    )
                sequence = connection.scalar(
                    text("SELECT pg_get_serial_sequence(:table, 'id')"),
                    {"table": f"public.{table}"},
                )
                assert sequence is not None
                assert connection.scalar(
                    text(
                        "SELECT has_sequence_privilege("
                        "current_user, :sequence, 'USAGE')"
                    ),
                    {"sequence": sequence},
                )
            transaction = connection.begin_nested()
            try:
                position_id = connection.scalar(
                    text(
                        "INSERT INTO positions(name) "
                        "VALUES ('App privilege test') RETURNING id"
                    )
                )
                connection.execute(
                    text(
                        "UPDATE positions SET name = 'Updated privilege test' "
                        "WHERE id=:id"
                    ),
                    {"id": position_id},
                )
                connection.execute(
                    text("DELETE FROM positions WHERE id=:id"), {"id": position_id}
                )
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def test_initial_domain_migration_on_disposable_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upgrade, inspect, exercise, and downgrade only a fresh test database."""
    migration_url = os.environ["TEST_MIGRATION_DATABASE_URL"]
    app_url = os.environ["TEST_DATABASE_URL"]
    _check_disposable_urls(migration_url, app_url)
    engine = create_engine(migration_url)
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    monkeypatch.setenv("MIGRATION_DATABASE_URL", migration_url)
    upgraded = False
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_user")) == (
                "dreamplanner_migration"
            )
            _reject_existing_schema(connection)
        command.upgrade(config, "head")
        upgraded = True
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == ("20261023_01")
            _check_catalog(connection)
        with engine.connect() as connection:
            _check_database_rules(connection)
        _check_app_privileges(app_url)
    finally:
        if upgraded:
            command.downgrade(config, "base")
            with engine.connect() as connection:
                assert set(inspect(connection).get_table_names()) == {"alembic_version"}
        engine.dispose()
