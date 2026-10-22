"""Check domain ORM metadata against the reference PostgreSQL schema."""

from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import CreateIndex

from src.assignments.models import ShiftAssignment
from src.core.models import Base

TIMESTAMPTZ = "TIMESTAMP WITH TIME ZONE"
LOCAL_TIME = "TIME WITHOUT TIME ZONE"

EXPECTED_COLUMNS = {
    "roles": {
        "id": "SMALLINT",
        "code": "VARCHAR(20)",
        "name": "VARCHAR(80)",
    },
    "users": {
        "id": "BIGINT",
        "role_id": "SMALLINT",
        "email": "VARCHAR(255)",
        "full_name": "VARCHAR(150)",
        "password_hash": "TEXT",
        "is_active": "BOOLEAN",
        "created_at": TIMESTAMPTZ,
        "updated_at": TIMESTAMPTZ,
        "activated_at": TIMESTAMPTZ,
    },
    "positions": {
        "id": "BIGINT",
        "name": "VARCHAR(120)",
        "is_active": "BOOLEAN",
    },
    "schedules": {
        "id": "BIGINT",
        "year": "INTEGER",
        "month": "INTEGER",
        "status": "VARCHAR(30)",
        "created_by": "BIGINT",
        "created_at": TIMESTAMPTZ,
        "collection_opened_at": TIMESTAMPTZ,
        "collection_closed_at": TIMESTAMPTZ,
        "published_at": TIMESTAMPTZ,
    },
    "shift_requirements": {
        "id": "BIGINT",
        "schedule_id": "BIGINT",
        "position_id": "BIGINT",
        "work_date": "DATE",
        "start_time": LOCAL_TIME,
        "end_time": LOCAL_TIME,
        "required_workers": "INTEGER",
        "note": "TEXT",
    },
    "availability_submissions": {
        "id": "BIGINT",
        "schedule_id": "BIGINT",
        "user_id": "BIGINT",
        "submitted_at": TIMESTAMPTZ,
        "created_at": TIMESTAMPTZ,
        "updated_at": TIMESTAMPTZ,
    },
    "availability_entries": {
        "id": "BIGINT",
        "submission_id": "BIGINT",
        "work_date": "DATE",
        "start_time": LOCAL_TIME,
        "end_time": LOCAL_TIME,
        "status": "VARCHAR(15)",
        "note": "TEXT",
    },
    "shift_assignments": {
        "id": "BIGINT",
        "shift_requirement_id": "BIGINT",
        "user_id": "BIGINT",
        "assigned_by": "BIGINT",
        "assigned_at": TIMESTAMPTZ,
        "warning_type": "VARCHAR(20)",
        "warning_acknowledged_at": TIMESTAMPTZ,
    },
}

EXPECTED_NULLABLE = {
    "users": {"activated_at"},
    "schedules": {
        "collection_opened_at",
        "collection_closed_at",
        "published_at",
    },
    "shift_requirements": {"note"},
    "availability_submissions": {"submitted_at"},
    "availability_entries": {"note"},
    "shift_assignments": {"warning_type", "warning_acknowledged_at"},
}

EXPECTED_DEFAULTS = {
    "users": {"is_active": "true", "created_at": "now()", "updated_at": "now()"},
    "positions": {"is_active": "true"},
    "schedules": {"status": "'draft'", "created_at": "now()"},
    "availability_submissions": {"created_at": "now()", "updated_at": "now()"},
    "shift_assignments": {"assigned_at": "now()"},
}

EXPECTED_FOREIGN_KEYS = {
    "users": {("role_id", "roles.id", None)},
    "schedules": {("created_by", "users.id", None)},
    "shift_requirements": {
        ("schedule_id", "schedules.id", "CASCADE"),
        ("position_id", "positions.id", None),
    },
    "availability_submissions": {
        ("schedule_id", "schedules.id", "CASCADE"),
        ("user_id", "users.id", None),
    },
    "availability_entries": {
        ("submission_id", "availability_submissions.id", "CASCADE")
    },
    "shift_assignments": {
        ("shift_requirement_id", "shift_requirements.id", "CASCADE"),
        ("user_id", "users.id", None),
        ("assigned_by", "users.id", None),
    },
}

EXPECTED_UNIQUES = {
    "roles": {(None, ("code",))},
    "positions": {(None, ("name",))},
    "schedules": {("uq_schedules_period", ("year", "month"))},
    "shift_requirements": {
        (
            "uq_shift_requirement",
            ("schedule_id", "position_id", "work_date", "start_time", "end_time"),
        )
    },
    "availability_submissions": {
        ("uq_availability_submission", ("schedule_id", "user_id"))
    },
    "availability_entries": {
        (
            "uq_availability_entry",
            ("submission_id", "work_date", "start_time", "end_time"),
        )
    },
    "shift_assignments": {("uq_shift_assignment", ("shift_requirement_id", "user_id"))},
}

EXPECTED_CHECKS = {
    "roles": {(None, "codein('admin','manager','employee')")},
    "users": {
        ("users_email_not_blank", "length(btrim(email))>0"),
        (
            "chk_users_active_requires_activation",
            "notis_activeoractivated_atisnotnull",
        ),
    },
    "positions": {("positions_name_not_blank", "length(btrim(name))>0")},
    "schedules": {
        (None, "yearbetween2000and2100"),
        (None, "monthbetween1and12"),
        (
            None,
            "statusin('draft','collecting','collection_closed','closed','published')",
        ),
    },
    "shift_requirements": {
        (None, "required_workers>0"),
        ("chk_shift_same_day", "start_time<end_time"),
    },
    "availability_entries": {
        (None, "statusin('available','unavailable')"),
        ("chk_availability_same_day", "start_time<end_time"),
    },
    "shift_assignments": {
        (None, "warning_typein('unavailable','unknown')"),
        (
            "chk_warning_ack_pair",
            "(warning_typeisnullandwarning_acknowledged_atisnull)or"
            "(warning_typeisnotnullandwarning_acknowledged_atisnotnull)",
        ),
    },
}

EXPECTED_INDEXES = {
    "users": {
        "ux_users_email_lower": "CREATE UNIQUE INDEX ux_users_email_lower ON users "
        "(lower(email))"
    },
    "shift_requirements": {
        "ix_shift_requirements_schedule_date": "CREATE INDEX "
        "ix_shift_requirements_schedule_date ON shift_requirements "
        "(schedule_id, work_date)"
    },
    "availability_submissions": {
        "ix_availability_submissions_user": "CREATE INDEX "
        "ix_availability_submissions_user ON availability_submissions (user_id)"
    },
    "availability_entries": {
        "ix_availability_entries_submission_date": "CREATE INDEX "
        "ix_availability_entries_submission_date ON availability_entries "
        "(submission_id, work_date)"
    },
    "shift_assignments": {
        "ix_shift_assignments_user": "CREATE INDEX "
        "ix_shift_assignments_user ON shift_assignments (user_id)"
    },
}


def test_all_reference_tables_and_columns_are_registered() -> None:
    """Alembic must see the domain tables and their SQL types."""
    assert set(EXPECTED_COLUMNS) <= set(Base.metadata.tables)
    dialect = postgresql.dialect()
    for table_name, expected_columns in EXPECTED_COLUMNS.items():
        table = Base.metadata.tables[table_name]
        assert {
            column.name: column.type.compile(dialect=dialect) for column in table.c
        } == (expected_columns)
        assert {column.name for column in table.c if column.nullable} == (
            EXPECTED_NULLABLE.get(table_name, set())
        )


def test_primary_keys_identity_and_server_defaults() -> None:
    """Identity modes and database defaults must match the reference SQL."""
    for table_name in EXPECTED_COLUMNS:
        table = Base.metadata.tables[table_name]
        assert tuple(column.name for column in table.primary_key.columns) == ("id",)
        assert table.c.id.identity is not None
        assert table.c.id.identity.always is (table_name != "roles")
        actual_defaults = {
            column.name: str(column.server_default.arg)
            for column in table.c
            if column.server_default is not None and column.identity is None
        }
        assert actual_defaults == EXPECTED_DEFAULTS.get(table_name, {})


def test_foreign_keys_and_delete_actions() -> None:
    """Every declared reference must target the correct key and delete action."""
    for table_name in EXPECTED_COLUMNS:
        table = Base.metadata.tables[table_name]
        actual = {
            (fk.parent.name, fk.target_fullname, fk.ondelete)
            for fk in table.foreign_keys
        }
        assert actual == EXPECTED_FOREIGN_KEYS.get(table_name, set())


def test_unique_and_check_constraints() -> None:
    """Unique keys and check expressions must retain reference semantics."""
    for table_name in EXPECTED_COLUMNS:
        table = Base.metadata.tables[table_name]
        uniques = {
            (constraint.name, tuple(column.name for column in constraint.columns))
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        checks = {
            (constraint.name, "".join(str(constraint.sqltext).split()).lower())
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        assert uniques == EXPECTED_UNIQUES.get(table_name, set())
        assert checks == EXPECTED_CHECKS.get(table_name, set())


def test_reference_indexes_compile_for_postgresql() -> None:
    """Check indexed columns and the case-insensitive email expression."""
    dialect = postgresql.dialect()
    for table_name in EXPECTED_COLUMNS:
        table = Base.metadata.tables[table_name]
        indexes = {
            index.name: str(CreateIndex(index).compile(dialect=dialect))
            for index in table.indexes
        }
        assert indexes == EXPECTED_INDEXES.get(table_name, {})


def test_relationships_resolve_with_two_distinct_user_foreign_keys() -> None:
    """Mapper configuration must resolve all modules without ambiguous users."""
    configure_mappers()
    relationships = ShiftAssignment.__mapper__.relationships
    assert {column.name for column in relationships["user"].local_columns} == {
        "user_id"
    }
    assert {column.name for column in relationships["assigner"].local_columns} == {
        "assigned_by"
    }
