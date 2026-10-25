"""PostgreSQL integrity and ordered availability submission races."""

import base64
import os
import secrets
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from datetime import time as local_time
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from src.auth import session_service
from src.availability.models import AvailabilityEntry, AvailabilitySubmission
from src.availability.repository import AvailabilitySubmissionRepository
from src.availability.schemas import (
    AvailabilityEntryInput,
    UpdateAvailabilitySubmissionInput,
)
from src.availability.service import (
    AvailabilityScheduleStateConflict,
    AvailabilitySubmissionAlreadySubmitted,
    AvailabilitySubmissionConflict,
    AvailabilitySubmissionNotFound,
    _entry_conflict,
    _submission_conflict,
    create_availability_submission,
    delete_availability_submission,
    update_availability_submission,
)
from src.core.config import Settings
from src.core.database import get_session
from src.main import create_app
from src.schedules.models import Schedule
from src.schedules.service import close_collection
from src.users.models import Role, User

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("TEST_DATABASE_URL")
        or not os.getenv("TEST_MIGRATION_DATABASE_URL"),
        reason="Set both TEST_*_DATABASE_URL values for the dedicated database",
    ),
]
DB_NAME = "dreamplanner_test_availability_117"
_MONTH = 0
_ERRORS = (
    AvailabilityScheduleStateConflict,
    AvailabilitySubmissionAlreadySubmitted,
    AvailabilitySubmissionConflict,
    AvailabilitySubmissionNotFound,
)


@pytest.fixture(scope="module")
def postgres_factory() -> Iterator[sessionmaker[Session]]:
    """Upgrade and downgrade only a verified empty, dedicated local database."""
    migration_url = os.environ["TEST_MIGRATION_DATABASE_URL"]
    app_url = os.environ["TEST_DATABASE_URL"]
    migration, app = make_url(migration_url), make_url(app_url)
    assert migration.drivername == app.drivername == "postgresql+psycopg"
    assert migration.host in {"127.0.0.1", "localhost"}
    assert (migration.host, migration.port, migration.database) == (
        app.host,
        app.port,
        app.database,
    )
    assert migration.database == DB_NAME
    assert migration.username == "dreamplanner_migration"
    assert app.username == "dreamplanner_app"
    assert DB_NAME not in {
        make_url(url).database
        for url in (Settings().database_url, Settings().migration_database_url)
        if url
    }
    migration_engine = create_engine(migration_url)
    app_engine = create_engine(
        app_url,
        connect_args={"options": "-c lock_timeout=8000 -c statement_timeout=20000"},
    )
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    upgraded = False
    try:
        with migration_engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
            assert tables <= {"alembic_version"}
            if "alembic_version" in tables:
                assert (
                    connection.scalar(text("SELECT count(*) FROM alembic_version")) == 0
                )
            assert connection.scalar(text("SELECT current_database()")) == DB_NAME
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                        "current_database() AND pid <> pg_backend_pid()"
                    )
                )
                == 0
            )
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("MIGRATION_DATABASE_URL", migration_url)
            command.upgrade(config, "head")
            upgraded = True
            yield sessionmaker(bind=app_engine, autoflush=False)
            command.downgrade(config, "base")
            upgraded = False
    finally:
        if upgraded:
            with pytest.MonkeyPatch.context() as patch:
                patch.setenv("MIGRATION_DATABASE_URL", migration_url)
                command.downgrade(config, "base")
        app_engine.dispose()
        migration_engine.dispose()


@pytest.fixture
def data(postgres_factory: sessionmaker[Session]) -> tuple[int, int, int, date]:
    """Create a distinct schedule and two active employees."""
    global _MONTH
    _MONTH += 1
    year, month = 2040 + _MONTH // 12, _MONTH % 12 + 1
    with postgres_factory() as session:
        role_id = session.scalar(select(Role.id).where(Role.code == "employee"))
        users = [
            User(
                role_id=role_id,
                email=f"availability-{uuid4()}@example.test",
                full_name="Availability Test Employee",
                password_hash="unused",
                is_active=True,
                activated_at=datetime.now(UTC),
            )
            for _ in range(2)
        ]
        schedule = Schedule(
            year=year,
            month=month,
            status="collecting",
            creator=users[0],
            collection_opened_at=datetime.now(UTC),
        )
        session.add_all([*users, schedule])
        session.commit()
        return schedule.id, users[0].id, users[1].id, date(year, month, 5)


def _entry(day: date, start: int = 8, end: int = 12, status: str = "available"):
    return AvailabilityEntryInput.model_validate(
        {
            "work_date": day.isoformat(),
            "start_time": f"{start:02}:00:00",
            "end_time": f"{end:02}:00:00",
            "status": status,
        }
    )


def _create(factory, schedule_id, user_id, entries, submit=False):
    with factory() as session:
        return create_availability_submission(
            session, schedule_id, user_id, entries=entries, submit=submit
        )


def _patch(factory, schedule_id, submission_id, user_id, **changes):
    with factory() as session:
        return update_availability_submission(
            session,
            schedule_id,
            submission_id,
            user_id,
            changes=UpdateAvailabilitySubmissionInput.model_validate(changes),
        )


def _delete(factory, schedule_id, submission_id, user_id):
    with factory() as session:
        return delete_availability_submission(
            session, schedule_id, submission_id, user_id
        )


def _count(factory, model, **filters):
    with factory() as session:
        return session.scalar(
            select(func.count()).select_from(model).filter_by(**filters)
        )


def _db_error(session, statement, params, state, constraint=None):
    with pytest.raises(DBAPIError) as error:
        with session.begin_nested():
            session.execute(text(statement), params)
    assert error.value.orig.sqlstate == state
    if constraint is not None:
        assert error.value.orig.diag.constraint_name == constraint
    assert session.scalar(text("SELECT 1")) == 1
    return error.value


def _raw_entry(session, submission_id, day, start=8, end=12, status="available"):
    return session.execute(
        text(
            "INSERT INTO availability_entries "
            "(submission_id,work_date,start_time,end_time,status) "
            "VALUES (:sid,:day,:start,:end,:status) RETURNING id"
        ),
        {
            "sid": submission_id,
            "day": day,
            "start": local_time(start),
            "end": local_time(end),
            "status": status,
        },
    ).scalar_one()


def _wait_blocked(factory, waiting_pid, blocking_pid):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        with factory() as session:
            blockers = session.scalar(
                text("SELECT pg_blocking_pids(:pid)"), {"pid": waiting_pid}
            )
        if blocking_pid in blockers:
            return
        time.sleep(0.02)
    pytest.fail("Second connection did not wait for the first PostgreSQL lock")


def _ordered(
    factory, first: Callable[[Session], object], second: Callable[[Session], object]
):
    """Hold the first commit and prove the second connection waits on its lock."""
    ready, connected, release = Event(), Event(), Event()
    pids = {}

    def run(operation, label):
        with factory() as session:
            pids[label] = session.scalar(text("SELECT pg_backend_pid()"))
            if label == "first":

                def pause(_session):
                    ready.set()
                    assert release.wait(timeout=12)

                event.listen(session, "before_commit", pause, once=True)
            else:
                connected.set()
            try:
                return operation(session)
            except _ERRORS as exc:
                return type(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result = pool.submit(run, first, "first")
        try:
            assert ready.wait(timeout=8)
            second_result = pool.submit(run, second, "second")
            assert connected.wait(timeout=8)
            _wait_blocked(factory, pids["second"], pids["first"])
        finally:
            release.set()
        return first_result.result(timeout=20), second_result.result(timeout=20)


def test_schema_metadata_and_constraints(postgres_factory):
    with postgres_factory() as session:
        columns = session.execute(
            text("""
            SELECT table_name,column_name,data_type,is_identity,is_nullable
            FROM information_schema.columns
            WHERE table_name IN ('availability_submissions','availability_entries')
        """)
        ).all()
        by_column = {(r.table_name, r.column_name): r for r in columns}
        for table in ("availability_submissions", "availability_entries"):
            assert by_column[table, "id"].data_type == "bigint"
            assert by_column[table, "id"].is_identity == "YES"
        assert (
            by_column["availability_submissions", "submitted_at"].is_nullable == "YES"
        )
        for field in ("created_at", "updated_at"):
            assert by_column["availability_submissions", field].is_nullable == "NO"
        constraints = session.execute(
            text("""
            SELECT conname, pg_get_constraintdef(oid) AS definition
            FROM pg_constraint WHERE conrelid IN
            ('availability_submissions'::regclass,'availability_entries'::regclass)
        """)
        ).all()
        defs = {r.conname: r.definition for r in constraints}
        assert "UNIQUE (schedule_id, user_id)" in defs["uq_availability_submission"]
        assert (
            "UNIQUE (submission_id, work_date, start_time, end_time)"
            in defs["uq_availability_entry"]
        )
        assert "CHECK ((start_time < end_time))" in defs["chk_availability_same_day"]
        assert any(
            "status" in name
            and "available" in definition
            and "unavailable" in definition
            for name, definition in defs.items()
        )
        for target, source in (
            ("schedules", "schedule_id"),
            ("users", "user_id"),
            ("availability_submissions", "submission_id"),
        ):
            assert any(
                f"FOREIGN KEY ({source}) REFERENCES {target}(id)" in definition
                for definition in defs.values()
            )
        indexes = {
            r[0]
            for r in session.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename IN "
                    "('availability_submissions','availability_entries')"
                )
            )
        }
        assert {
            "ix_availability_submissions_user",
            "ix_availability_entries_submission_date",
            "uq_availability_submission",
            "uq_availability_entry",
        } <= indexes
        triggers = {
            r[0]
            for r in session.execute(
                text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgrelid='availability_entries'::regclass "
                    "AND NOT tgisinternal"
                )
            )
        }
        assert {"trg_availability_period", "trg_availability_overlap"} <= triggers


def test_foreign_keys_checks_and_unique(postgres_factory, data):
    sid, uid, other_uid, day = data
    first = _create(postgres_factory, sid, uid, [])
    with postgres_factory() as session:
        _db_error(
            session,
            "INSERT INTO availability_submissions(schedule_id,user_id) "
            "VALUES (:sid,:uid)",
            {"sid": sid, "uid": uid},
            "23505",
            "uq_availability_submission",
        )
        assert _submission_conflict(
            _db_error(
                session,
                "INSERT INTO availability_submissions(schedule_id,user_id) "
                "VALUES (:sid,:uid)",
                {"sid": sid, "uid": uid},
                "23505",
            )
        )
        unrelated = _db_error(
            session,
            "INSERT INTO availability_submissions(schedule_id,user_id) "
            "VALUES (-1,:uid)",
            {"uid": other_uid},
            "23503",
        )
        assert not _submission_conflict(unrelated)
        assert not _entry_conflict(unrelated)
        _db_error(
            session,
            "INSERT INTO availability_submissions(schedule_id,user_id) "
            "VALUES (:sid,-1)",
            {"sid": sid},
            "23503",
        )
        _db_error(
            session,
            "INSERT INTO availability_entries "
            "(submission_id,work_date,start_time,end_time,status) "
            "VALUES (-1,:day,'08:00','12:00','available')",
            {"day": day},
            "P0001",
        )
        eid = _raw_entry(session, int(first.id), day)
        overlap = _db_error(
            session,
            "INSERT INTO availability_entries "
            "(submission_id,work_date,start_time,end_time,status) "
            "VALUES (:sid,:day,'08:00','12:00','available')",
            {"sid": int(first.id), "day": day},
            "P0001",
        )
        assert _entry_conflict(overlap)
        _db_error(
            session,
            "UPDATE availability_entries SET status='maybe' WHERE id=:id",
            {"id": eid},
            "23514",
        )
        _db_error(
            session,
            "UPDATE availability_entries SET end_time='07:00' WHERE id=:id",
            {"id": eid},
            "23514",
            "chk_availability_same_day",
        )
        session.rollback()


def test_period_trigger_on_insert_and_update(postgres_factory, data):
    sid, uid, other_uid, day = data
    first = _create(postgres_factory, sid, uid, [])
    with postgres_factory() as session:
        second_schedule = Schedule(
            year=day.year,
            month=day.month % 12 + 1,
            creator=session.get(User, uid),
            status="collecting",
        )
        session.add(second_schedule)
        session.flush()
        second = AvailabilitySubmission(
            schedule_id=second_schedule.id, user_id=other_uid
        )
        session.add(second)
        session.flush()
        eid = _raw_entry(session, int(first.id), day)
        for wrong in (
            date(day.year, day.month, 1) - timedelta(days=1),
            date(day.year, day.month % 12 + 1, 1),
        ):
            error = _db_error(
                session,
                "INSERT INTO availability_entries "
                "(submission_id,work_date,start_time,end_time,status) "
                "VALUES (:sid,:day,'12:00','14:00','available')",
                {"sid": int(first.id), "day": wrong},
                "P0001",
            )
            assert "musi nalezec" in error.orig.diag.message_primary
            _db_error(
                session,
                "UPDATE availability_entries SET work_date=:day WHERE id=:id",
                {"day": wrong, "id": eid},
                "P0001",
            )
        _db_error(
            session,
            "UPDATE availability_entries SET submission_id=:sid WHERE id=:id",
            {"sid": second.id, "id": eid},
            "P0001",
        )
        session.rollback()


@pytest.mark.parametrize(
    "start,end,allowed",
    [
        (12, 14, True),
        (14, 16, True),
        (10, 14, False),
        (7, 13, False),
        (8, 12, False),
    ],
)
def test_overlap_trigger(postgres_factory, data, start, end, allowed):
    sid, uid, other_uid, day = data
    first = _create(postgres_factory, sid, uid, [])
    second = _create(postgres_factory, sid, other_uid, [])
    with postgres_factory() as session:
        _raw_entry(session, int(first.id), day)
        if allowed:
            _raw_entry(session, int(first.id), day, start, end)
        else:
            with pytest.raises(DBAPIError) as error:
                with session.begin_nested():
                    _raw_entry(session, int(first.id), day, start, end, "unavailable")
            assert error.value.orig.sqlstate == "P0001"
            assert (
                error.value.orig.diag.message_primary
                == "Nakladajace sie przedzialy dyspozycyjnosci"
            )
            assert _entry_conflict(error.value)
        _raw_entry(session, int(second.id), day)
        _raw_entry(session, int(first.id), day + timedelta(days=1))
        session.rollback()


def test_fk_cascades_and_user_restriction(postgres_factory, data):
    sid, uid, other_uid, day = data
    first = _create(postgres_factory, sid, uid, [_entry(day)])
    with postgres_factory() as session:
        _db_error(session, "DELETE FROM users WHERE id=:id", {"id": uid}, "23503")
        session.execute(
            text("DELETE FROM availability_submissions WHERE id=:id"),
            {"id": int(first.id)},
        )
        session.commit()
    assert _count(postgres_factory, AvailabilityEntry, submission_id=int(first.id)) == 0
    second = _create(postgres_factory, sid, other_uid, [_entry(day)])
    with postgres_factory() as session:
        session.execute(text("DELETE FROM schedules WHERE id=:id"), {"id": sid})
        session.commit()
    assert _count(postgres_factory, AvailabilitySubmission, id=int(second.id)) == 0
    assert (
        _count(postgres_factory, AvailabilityEntry, submission_id=int(second.id)) == 0
    )


def test_post_patch_delete_and_rollback(postgres_factory, data):
    sid, uid, _, day = data
    created = _create(postgres_factory, sid, uid, [_entry(day), _entry(day, 12, 16)])
    assert len(created.entries) == 2
    with pytest.raises(AvailabilitySubmissionConflict):
        _create(postgres_factory, sid, uid, [])
    old_ids = {int(entry.id) for entry in created.entries}
    replaced = _patch(
        postgres_factory,
        sid,
        int(created.id),
        uid,
        entries=[_entry(day, 9, 13).model_dump(mode="json")],
        submit=True,
    )
    assert len(replaced.entries) == 1
    assert int(replaced.entries[0].id) not in old_ids
    assert replaced.submitted_at is not None
    resubmitted = _patch(postgres_factory, sid, int(created.id), uid, submit=True)
    assert [e.id for e in resubmitted.entries] == [e.id for e in replaced.entries]
    draft = _patch(postgres_factory, sid, int(created.id), uid, entries=[])
    assert draft.submitted_at is None and draft.entries == []
    assert (
        _count(postgres_factory, AvailabilityEntry, submission_id=int(created.id)) == 0
    )
    assert _delete(postgres_factory, sid, int(created.id), uid) is None
    with pytest.raises(AvailabilitySubmissionNotFound):
        _delete(postgres_factory, sid, int(created.id), uid)
    assert _count(postgres_factory, AvailabilitySubmission, id=int(created.id)) == 0


@pytest.mark.parametrize("operation", ["post", "patch", "delete"])
def test_write_against_close_in_both_orders(postgres_factory, data, operation):
    sid, uid, other_uid, day = data
    target = (
        None
        if operation == "post"
        else _create(postgres_factory, sid, uid, [_entry(day)])
    )

    def write(session):
        if operation == "post":
            return create_availability_submission(
                session, sid, other_uid, entries=[_entry(day)], submit=False
            )
        if operation == "patch":
            return update_availability_submission(
                session,
                sid,
                int(target.id),
                uid,
                changes=UpdateAvailabilitySubmissionInput.model_validate(
                    {"entries": [_entry(day, 12, 16).model_dump(mode="json")]}
                ),
            )
        return delete_availability_submission(session, sid, int(target.id), uid)

    def close(session):
        return close_collection(session, sid).status

    first, second = _ordered(postgres_factory, write, close)
    assert not isinstance(first, type) and second == "collection_closed"
    with postgres_factory() as session:
        session.get(Schedule, sid).status = "collecting"
        session.commit()
    before = _count(postgres_factory, AvailabilitySubmission, schedule_id=sid)
    first, second = _ordered(postgres_factory, close, write)
    assert first == "collection_closed"
    assert second is AvailabilityScheduleStateConflict
    assert _count(postgres_factory, AvailabilitySubmission, schedule_id=sid) == before


@pytest.mark.parametrize("same_user", [True, False])
def test_ordered_post_post(postgres_factory, data, same_user):
    sid, uid, other_uid, day = data

    def post(session, user):
        return create_availability_submission(
            session, sid, user, entries=[_entry(day)], submit=False
        )

    first, second = _ordered(
        postgres_factory,
        lambda s: post(s, uid),
        lambda s: post(s, uid if same_user else other_uid),
    )
    assert not isinstance(first, type)
    assert (second is AvailabilitySubmissionConflict) == same_user
    assert _count(postgres_factory, AvailabilitySubmission, schedule_id=sid) == (
        1 if same_user else 2
    )


@pytest.mark.parametrize(
    "first_op,second_op", [("patch", "patch"), ("patch", "delete"), ("delete", "patch")]
)
def test_ordered_patch_delete(postgres_factory, data, first_op, second_op):
    sid, uid, _, day = data
    created = _create(postgres_factory, sid, uid, [_entry(day)])

    def operation(session, kind, start):
        if kind == "delete":
            return delete_availability_submission(session, sid, int(created.id), uid)
        return update_availability_submission(
            session,
            sid,
            int(created.id),
            uid,
            changes=UpdateAvailabilitySubmissionInput.model_validate(
                {"entries": [_entry(day, start, start + 4).model_dump(mode="json")]}
            ),
        )

    first, second = _ordered(
        postgres_factory,
        lambda s: operation(s, first_op, 12),
        lambda s: operation(s, second_op, 16),
    )
    assert not isinstance(first, type)
    if first_op == "delete":
        assert second is AvailabilitySubmissionNotFound
    else:
        assert not isinstance(second, type)
    assert _count(
        postgres_factory, AvailabilityEntry, submission_id=int(created.id)
    ) == (1 if second_op == "patch" and first_op != "delete" else 0)
    if first_op == second_op == "patch":
        with postgres_factory() as session:
            current = session.scalar(
                select(AvailabilityEntry).where(
                    AvailabilityEntry.submission_id == int(created.id)
                )
            )
            assert current.start_time == local_time(16)


@pytest.mark.parametrize("operation", ["post", "patch"])
def test_failed_entry_flush_rolls_back_entire_write(
    postgres_factory, data, monkeypatch, operation
):
    sid, uid, _, day = data
    old = None
    if operation == "patch":
        old = _create(postgres_factory, sid, uid, [_entry(day)], submit=True)
    original = AvailabilitySubmissionRepository.add_entries

    def invalid_entries(repository, entries):
        original(repository, entries)
        repository.session.add(
            AvailabilityEntry(
                submission_id=entries[0].submission_id,
                work_date=day,
                start_time=local_time(17),
                end_time=local_time(18),
                status="invalid",
            )
        )

    monkeypatch.setattr(
        AvailabilitySubmissionRepository, "add_entries", invalid_entries
    )
    with postgres_factory() as session:
        with pytest.raises(IntegrityError) as error:
            if operation == "post":
                create_availability_submission(
                    session, sid, uid, entries=[_entry(day)], submit=False
                )
            else:
                update_availability_submission(
                    session,
                    sid,
                    int(old.id),
                    uid,
                    changes=UpdateAvailabilitySubmissionInput.model_validate(
                        {"entries": [_entry(day, 9, 13).model_dump(mode="json")]}
                    ),
                )
        assert error.value.orig.sqlstate == "23514"
        assert session.scalar(text("SELECT 1")) == 1
    if old is None:
        assert _count(postgres_factory, AvailabilitySubmission, schedule_id=sid) == 0
        with postgres_factory() as session:
            assert (
                session.scalar(
                    text("""
                SELECT count(*) FROM availability_entries e
                LEFT JOIN availability_submissions s ON s.id=e.submission_id
                WHERE s.id IS NULL
            """)
                )
                == 0
            )
    else:
        with postgres_factory() as session:
            current = session.get(AvailabilitySubmission, int(old.id))
            assert current.submitted_at == old.submitted_at
            assert current.updated_at == old.updated_at
            entries = session.scalars(
                select(AvailabilityEntry).where(
                    AvailabilityEntry.submission_id == int(old.id)
                )
            ).all()
            assert len(entries) == 1 and str(entries[0].id) == old.entries[0].id


def test_failed_delete_commit_rolls_back_and_orm_uses_fk_cascade(
    postgres_factory, data
):
    sid, uid, _, day = data
    created = _create(postgres_factory, sid, uid, [_entry(day)])
    with postgres_factory() as session:

        def fail(_session):
            raise RuntimeError("injected commit failure")

        event.listen(session, "before_commit", fail, once=True)
        with pytest.raises(RuntimeError, match="injected commit failure"):
            delete_availability_submission(session, sid, int(created.id), uid)
        assert session.scalar(text("SELECT 1")) == 1
    assert _count(postgres_factory, AvailabilitySubmission, id=int(created.id)) == 1
    assert (
        _count(postgres_factory, AvailabilityEntry, submission_id=int(created.id)) == 1
    )

    statements = []
    engine = postgres_factory.kw["bind"]

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if "DELETE FROM" in statement.upper():
            statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", observe)
    try:
        _delete(postgres_factory, sid, int(created.id), uid)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert len(statements) == 1
    assert "availability_submissions" in statements[0]
    assert (
        _count(postgres_factory, AvailabilityEntry, submission_id=int(created.id)) == 0
    )


def test_unrelated_integrity_error_is_not_mapped_to_conflict(postgres_factory, data):
    sid, _, _, day = data
    with postgres_factory() as session:
        with pytest.raises(IntegrityError) as error:
            create_availability_submission(
                session, sid, -1, entries=[_entry(day)], submit=False
            )
        assert error.value.orig.sqlstate == "23503"
        assert session.scalar(text("SELECT 1")) == 1
    assert _count(postgres_factory, AvailabilitySubmission, schedule_id=sid) == 0


def test_lock_order_is_schedule_then_submission(postgres_factory, data):
    sid, uid, _, day = data
    created = _create(postgres_factory, sid, uid, [_entry(day)])
    statements = []
    engine = postgres_factory.kw["bind"]

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if "FOR UPDATE" in statement.upper():
            statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", observe)
    try:
        _patch(postgres_factory, sid, int(created.id), uid, submit=True)
        _patch(postgres_factory, sid, int(created.id), uid, entries=[])
        _delete(postgres_factory, sid, int(created.id), uid)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert len(statements) == 6
    for first, second in zip(statements[::2], statements[1::2], strict=True):
        assert "schedules" in first
        assert "availability_submissions" in second


@pytest.fixture
def api(postgres_factory, data, monkeypatch):
    sid, uid, _, day = data
    monkeypatch.setenv(
        "JWT_SIGNING_KEY_BASE64", base64.b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("JWT_CURRENT_KID", "availability-postgres-test")
    monkeypatch.setenv("JWT_ISSUER", "availability-postgres-test")
    monkeypatch.setenv("JWT_AUDIENCE", "availability-postgres-api")
    app = create_app()
    session_id = uuid4()
    actor = SimpleNamespace(
        id=uid,
        is_active=True,
        activated_at=datetime.now(UTC),
        role=SimpleNamespace(code="employee"),
    )
    auth_session = SimpleNamespace(
        user_id=uid,
        revoked_at=None,
        idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
        absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
    )

    class AuthRepository:
        def get_session(self, requested_id):
            return auth_session if requested_id == session_id else None

        def get_user_fresh(self, requested_id):
            return actor if requested_id == uid else None

    monkeypatch.setattr(
        session_service, "AuthSessionRepository", lambda session: AuthRepository()
    )

    def test_session():
        with postgres_factory() as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    yield app, app.state.jwt_codec.issue(uid, session_id), sid, day
    app.dependency_overrides.clear()


def test_http_lifecycle_and_problem_details(api, postgres_factory):
    app, token, sid, day = api
    path = f"/api/v1/schedules/{sid}/availability-submissions"
    headers = {"Authorization": f"Bearer {token}"}
    entry = _entry(day).model_dump(mode="json")
    with TestClient(app) as client:
        created = client.post(path, headers=headers, json={"entries": [entry]})
        assert created.status_code == 201
        submission_id = created.json()["id"]
        assert created.json()["entries"][0]["work_date"] == day.isoformat()
        assert client.get(path, headers=headers).json()["total"] == 1
        detail = client.get(f"{path}/{submission_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["id"] == submission_id
        conflict = client.post(path, headers=headers, json={"entries": []})
        assert conflict.status_code == 409
        assert conflict.headers["content-type"].startswith("application/problem+json")
        assert "sql" not in str(conflict.json()).lower()
        updated = client.patch(
            f"{path}/{submission_id}", headers=headers, json={"submit": True}
        )
        assert updated.status_code == 200
        assert updated.json()["submitted_at"] is not None
        denied = client.delete(f"{path}/{submission_id}", headers=headers)
        assert denied.status_code == 409
        assert (
            client.patch(
                f"{path}/{submission_id}", headers=headers, json={"entries": []}
            ).status_code
            == 200
        )
        assert (
            client.delete(f"{path}/{submission_id}", headers=headers).status_code == 204
        )
        assert (
            client.delete(f"{path}/{submission_id}", headers=headers).status_code == 404
        )
    assert (
        _count(postgres_factory, AvailabilityEntry, submission_id=int(submission_id))
        == 0
    )
