"""Create the reference domain schema.

Revision ID: 20261023_01
Revises:
"""

# ruff: noqa: E501

import sqlalchemy as sa

from alembic import op

revision = "20261023_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create domain tables, reference roles, functions, and triggers."""
    op.create_table(
        "roles",
        sa.Column("id", sa.SmallInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.CheckConstraint("code IN ('admin','manager','employee')"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.bulk_insert(
        sa.table("roles", sa.column("code", sa.String), sa.column("name", sa.String)),
        [
            {"code": "admin", "name": "Administrator"},
            {"code": "manager", "name": "Menadżer"},
            {"code": "employee", "name": "Pracownik"},
        ],
    )

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("role_id", sa.SmallInteger(), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(150), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(btrim(email)) > 0", name="users_email_not_blank"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_users_email_lower", "users", [sa.text("lower(email)")], unique=True
    )

    op.create_table(
        "positions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.CheckConstraint("length(btrim(name)) > 0", name="positions_name_not_blank"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "schedules",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(30), server_default=sa.text("'draft'"), nullable=False
        ),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("collection_opened_at", sa.DateTime(timezone=True)),
        sa.Column("collection_closed_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("year BETWEEN 2000 AND 2100"),
        sa.CheckConstraint("month BETWEEN 1 AND 12"),
        sa.CheckConstraint(
            "status IN ('draft','collecting','collection_closed','closed','published')"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("year", "month", name="uq_schedules_period"),
    )

    op.create_table(
        "shift_requirements",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("schedule_id", sa.BigInteger(), nullable=False),
        sa.Column("position_id", sa.BigInteger(), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("required_workers", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.CheckConstraint("required_workers > 0"),
        sa.CheckConstraint("start_time < end_time", name="chk_shift_same_day"),
        sa.ForeignKeyConstraint(["schedule_id"], ["schedules.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "schedule_id",
            "position_id",
            "work_date",
            "start_time",
            "end_time",
            name="uq_shift_requirement",
        ),
    )
    op.create_index(
        "ix_shift_requirements_schedule_date",
        "shift_requirements",
        ["schedule_id", "work_date"],
    )

    op.create_table(
        "availability_submissions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("schedule_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["schedule_id"], ["schedules.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "schedule_id", "user_id", name="uq_availability_submission"
        ),
    )
    op.create_index(
        "ix_availability_submissions_user", "availability_submissions", ["user_id"]
    )

    op.create_table(
        "availability_entries",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("submission_id", sa.BigInteger(), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("status", sa.String(15), nullable=False),
        sa.Column("note", sa.Text()),
        sa.CheckConstraint("status IN ('available','unavailable')"),
        sa.CheckConstraint("start_time < end_time", name="chk_availability_same_day"),
        sa.ForeignKeyConstraint(
            ["submission_id"], ["availability_submissions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "submission_id",
            "work_date",
            "start_time",
            "end_time",
            name="uq_availability_entry",
        ),
    )
    op.create_index(
        "ix_availability_entries_submission_date",
        "availability_entries",
        ["submission_id", "work_date"],
    )

    op.create_table(
        "shift_assignments",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("shift_requirement_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("assigned_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("warning_type", sa.String(20)),
        sa.Column("warning_acknowledged_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("warning_type IN ('unavailable','unknown')"),
        sa.CheckConstraint(
            "(warning_type IS NULL AND warning_acknowledged_at IS NULL) OR "
            "(warning_type IS NOT NULL AND warning_acknowledged_at IS NOT NULL)",
            name="chk_warning_ack_pair",
        ),
        sa.ForeignKeyConstraint(
            ["shift_requirement_id"], ["shift_requirements.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["assigned_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "shift_requirement_id", "user_id", name="uq_shift_assignment"
        ),
    )
    op.create_index("ix_shift_assignments_user", "shift_assignments", ["user_id"])

    op.execute(
        """
CREATE FUNCTION validate_schedule_dates() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE sy INTEGER; sm INTEGER; sid BIGINT;
BEGIN
  IF TG_TABLE_NAME = 'shift_requirements' THEN
    sid := NEW.schedule_id;
  ELSE
    SELECT schedule_id INTO sid FROM availability_submissions WHERE id = NEW.submission_id;
  END IF;
  SELECT year, month INTO sy, sm FROM schedules WHERE id = sid;
  IF sy IS NULL OR EXTRACT(YEAR FROM NEW.work_date)::int <> sy
     OR EXTRACT(MONTH FROM NEW.work_date)::int <> sm THEN
    RAISE EXCEPTION 'Data % musi nalezec do miesiaca grafiku %-%', NEW.work_date, sy, sm;
  END IF;
  RETURN NEW;
END;
$$
"""
    )
    op.execute(
        """CREATE TRIGGER trg_shift_period BEFORE INSERT OR UPDATE OF schedule_id,work_date
ON shift_requirements FOR EACH ROW EXECUTE FUNCTION validate_schedule_dates()"""
    )
    op.execute(
        """CREATE TRIGGER trg_availability_period BEFORE INSERT OR UPDATE OF submission_id,work_date
ON availability_entries FOR EACH ROW EXECUTE FUNCTION validate_schedule_dates()"""
    )

    op.execute(
        """
CREATE FUNCTION prevent_overlapping_availability() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS (
  SELECT 1 FROM availability_entries e
  WHERE e.submission_id=NEW.submission_id AND e.work_date=NEW.work_date
    AND e.id<>COALESCE(NEW.id,-1)
    AND NEW.start_time < e.end_time AND e.start_time < NEW.end_time
 ) THEN
  RAISE EXCEPTION 'Nakladajace sie przedzialy dyspozycyjnosci';
 END IF;
 RETURN NEW;
END;
$$
"""
    )
    op.execute(
        """CREATE TRIGGER trg_availability_overlap BEFORE INSERT OR UPDATE
ON availability_entries FOR EACH ROW EXECUTE FUNCTION prevent_overlapping_availability()"""
    )

    op.execute(
        """
CREATE FUNCTION validate_shift_assignment() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s shift_requirements%ROWTYPE;
BEGIN
 SELECT * INTO s FROM shift_requirements WHERE id=NEW.shift_requirement_id;
 IF EXISTS (
    SELECT 1 FROM shift_assignments a
    JOIN shift_requirements other_s ON other_s.id=a.shift_requirement_id
    WHERE a.user_id=NEW.user_id AND a.id<>COALESCE(NEW.id,-1)
      AND other_s.work_date=s.work_date
      AND s.start_time < other_s.end_time AND other_s.start_time < s.end_time
 ) THEN RAISE EXCEPTION 'Pracownik ma kolidujacy przydzial'; END IF;
 RETURN NEW;
END;
$$
"""
    )
    op.execute(
        """CREATE TRIGGER trg_assignment_overlap BEFORE INSERT OR UPDATE OF shift_requirement_id,user_id
ON shift_assignments FOR EACH ROW EXECUTE FUNCTION validate_shift_assignment()"""
    )


def downgrade() -> None:
    """Remove reference triggers, functions, indexes, and tables in reverse order."""
    op.execute("DROP TRIGGER trg_assignment_overlap ON shift_assignments")
    op.execute("DROP TRIGGER trg_availability_overlap ON availability_entries")
    op.execute("DROP TRIGGER trg_availability_period ON availability_entries")
    op.execute("DROP TRIGGER trg_shift_period ON shift_requirements")
    op.execute("DROP FUNCTION validate_shift_assignment()")
    op.execute("DROP FUNCTION prevent_overlapping_availability()")
    op.execute("DROP FUNCTION validate_schedule_dates()")

    op.drop_index("ix_shift_assignments_user", table_name="shift_assignments")
    op.drop_table("shift_assignments")
    op.drop_index(
        "ix_availability_entries_submission_date", table_name="availability_entries"
    )
    op.drop_table("availability_entries")
    op.drop_index(
        "ix_availability_submissions_user", table_name="availability_submissions"
    )
    op.drop_table("availability_submissions")
    op.drop_index(
        "ix_shift_requirements_schedule_date", table_name="shift_requirements"
    )
    op.drop_table("shift_requirements")
    op.drop_table("schedules")
    op.drop_table("positions")
    op.drop_index("ux_users_email_lower", table_name="users")
    op.drop_table("users")
    op.drop_table("roles")
