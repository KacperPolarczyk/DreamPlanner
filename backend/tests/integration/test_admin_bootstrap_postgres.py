"""Exercise administrator bootstrap on an isolated migrated PostgreSQL database."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from alembic import command
from src.auth.passwords import hash_password, verify_password
from src.core import database
from src.core.config import Settings
from src.users import bootstrap_admin
from src.users.models import Role, User
from src.users.service import bootstrap_administrator

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("TEST_MIGRATION_DATABASE_URL")
        or not os.getenv("TEST_DATABASE_URL"),
        reason="Set both TEST_*_DATABASE_URL values for a disposable local database",
    ),
]


def test_bootstrap_on_disposable_postgres(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify committed state, retry rejection, and rollback on real PostgreSQL."""
    migration_url = os.environ["TEST_MIGRATION_DATABASE_URL"]
    app_url = os.environ["TEST_DATABASE_URL"]
    migration = make_url(migration_url)
    app = make_url(app_url)
    assert migration.drivername == app.drivername == "postgresql+psycopg"
    assert migration.host in {"localhost", "127.0.0.1"}
    assert (migration.host, migration.port, migration.database) == (
        app.host,
        app.port,
        app.database,
    )
    assert migration.database is not None
    assert migration.database.startswith("dreamplanner_test_")
    assert migration.username == "dreamplanner_migration"
    assert app.username == "dreamplanner_app"
    assert migration.database not in {
        make_url(url).database
        for url in (Settings().database_url, Settings().migration_database_url)
        if url
    }

    migration_engine = create_engine(migration_url)
    app_engine = create_engine(app_url)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    monkeypatch.setenv("MIGRATION_DATABASE_URL", migration_url)
    monkeypatch.setenv("DATABASE_URL", app_url)
    upgraded = False
    try:
        with migration_engine.connect() as connection:
            assert not inspect(connection).has_table("users")
        command.upgrade(config, "head")
        upgraded = True
        factory = sessionmaker(bind=app_engine, autoflush=False, autocommit=False)
        with factory() as session:
            employee_role = session.scalar(select(Role).where(Role.code == "employee"))
            assert employee_role is not None
            session.add(
                User(
                    role_id=employee_role.id,
                    email="occupied@example.com",
                    full_name="Existing Employee",
                    password_hash=hash_password("seed password"),
                    is_active=False,
                )
            )
            session.commit()

        with factory() as session:
            with pytest.raises(IntegrityError):
                bootstrap_administrator(
                    session, "occupied@example.com", "Duplicate", "secret"
                )
            assert not session.in_transaction()
        with factory() as session:
            assert (
                session.scalar(select(User).join(Role).where(Role.code == "admin"))
                is None
            )

        before = datetime.now(UTC)
        database.get_session_factory.cache_clear()
        database.get_engine.cache_clear()
        answers = iter(["secret", "secret", "secret", "secret"])
        monkeypatch.setattr(bootstrap_admin.getpass, "getpass", lambda _: next(answers))
        args = ["--email", "ADMIN@Example.COM", "--full-name", " Anna Nowak "]
        assert bootstrap_admin.main(args) == 0
        with pytest.raises(SystemExit) as retry:
            bootstrap_admin.main(args)
        assert retry.value.code == 1
        output = capsys.readouterr()
        assert "secret" not in output.out + output.err + caplog.text

        with factory() as session:
            user = session.scalar(select(User).where(User.email == "ADMIN@example.com"))
            assert user is not None
            assert user.email == "ADMIN@example.com"
            assert user.full_name == "Anna Nowak"
            assert user.is_active is True
            assert user.activated_at is not None
            assert before <= user.activated_at <= datetime.now(UTC)
            assert user.role.code == "admin"
            assert user.password_hash.startswith("$argon2id$")
            assert verify_password("secret", user.password_hash)
            assert not verify_password("wrong", user.password_hash)

        with factory() as session:
            assert (
                session.scalar(select(User).where(User.email == "next@example.com"))
                is None
            )
            assert (
                session.scalar(select(Role.id).where(Role.code == "admin")) is not None
            )
        with migration_engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM users")) == 2
    finally:
        database.get_session_factory.cache_clear()
        if database.get_engine.cache_info().currsize:
            database.get_engine().dispose()
        database.get_engine.cache_clear()
        if upgraded:
            command.downgrade(config, "base")
        app_engine.dispose()
        migration_engine.dispose()
