"""Exercise real HTTP authentication and committed SQL writes as the runtime role."""

import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from alembic import command
from src.auth.models import AuthSession
from src.auth.passwords import hash_password
from src.core.database import get_engine, get_session_factory
from src.main import create_app
from src.positions.models import Position
from src.users.models import Role, User

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("TEST_DATABASE_URL")
        or not os.getenv("TEST_MIGRATION_DATABASE_URL"),
        reason="Requires disposable local PostgreSQL URLs",
    ),
]
ORIGIN = "http://localhost:5173"
POSITIONS = "/api/v1/positions"


@pytest.fixture(scope="module")
def database():
    """Migrate only the runner's dedicated database, using separate SQL roles."""
    app = make_url(os.environ["TEST_DATABASE_URL"])
    migration = make_url(os.environ["TEST_MIGRATION_DATABASE_URL"])
    assert app.drivername == migration.drivername == "postgresql+psycopg"
    assert app.host == migration.host == "127.0.0.1"
    assert app.port == migration.port
    assert (
        app.database == migration.database == "dreamplanner_test_http_database_postgres"
    )
    assert app.username == "dreamplanner_app"
    assert migration.username == "dreamplanner_migration"
    migration_engine = create_engine(migration)
    engine = create_engine(app)
    try:
        with migration_engine.connect() as conn:
            assert inspect(conn).get_table_names() == []
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv(
                "MIGRATION_DATABASE_URL", str(migration.render_as_string(False))
            )
            command.upgrade(
                Config(str(Path(__file__).parents[2] / "alembic.ini")), "head"
            )
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT current_user")) == "dreamplanner_app"
            assert not conn.scalar(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            )
        yield engine
    finally:
        engine.dispose()
        migration_engine.dispose()


@pytest.fixture
def api(database, monkeypatch):
    """Use real application dependencies, passwords, JWTs and database sessions."""
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setenv("CORS_ORIGINS", '["http://localhost:5173"]')
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("REFRESH_COOKIE_SECURE", "false")
    monkeypatch.setenv("REFRESH_COOKIE_SAMESITE", "lax")
    get_session_factory.cache_clear()
    get_engine.cache_clear()
    accounts = {}
    with Session(database) as session:
        for role in ("admin", "employee"):
            email = f"{role}-{uuid4().hex}@example.com"
            password = secrets.token_urlsafe(24)
            user = User(
                role_id=session.scalar(select(Role.id).where(Role.code == role)),
                email=email,
                full_name="Disposable HTTP database test",
                password_hash=hash_password(password),
                is_active=True,
                activated_at=datetime.now(UTC),
            )
            session.add(user)
            session.flush()
            accounts[role] = {"email": email, "password": password, "id": user.id}
        session.commit()
    application = create_app()
    try:
        with TestClient(application, headers={"Origin": ORIGIN}) as client:
            yield client, accounts, database, application
    finally:
        get_engine().dispose()
        get_session_factory.cache_clear()
        get_engine.cache_clear()


def login(client, account):
    response = client.post(
        "/api/v1/auth/sessions",
        json={"email": account["email"], "password": account["password"]},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def count(engine, name):
    with Session(engine) as session:
        return session.scalar(
            select(func.count()).select_from(Position).where(Position.name == name)
        )


def test_authenticated_write_survives_new_http_client_and_sql_connection(api):
    client, accounts, engine, application = api
    headers = login(client, accounts["admin"])
    name = "HTTP-" + uuid4().hex
    created = client.post(POSITIONS, headers=headers, json={"name": name})
    assert created.status_code == 201
    assert created.headers["content-type"] == "application/json"
    assert created.headers["cache-control"] == "no-store"
    payload = created.json()
    assert set(payload) == {"id", "name", "is_active"}
    assert isinstance(payload["id"], str) and payload["id"].isdigit()
    assert payload["name"] == name and payload["is_active"] is True
    assert created.headers["location"] == f"{POSITIONS}/{payload['id']}"
    assert count(engine, name) == 1
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuthSession)
                .where(AuthSession.user_id == accounts["admin"]["id"])
            )
            == 1
        )
    with TestClient(application) as fresh_client:
        read = fresh_client.get(created.headers["location"], headers=headers)
        assert read.status_code == 200
        assert read.json() == payload
        listing = fresh_client.get(POSITIONS, params={"q": name}, headers=headers)
        assert listing.status_code == 200
        assert listing.json()["items"] == [payload]
        health = fresh_client.get("/api/v1/health/ready")
        assert health.status_code == 200 and health.json() == {"status": "ok"}


def test_unique_conflict_rolls_back_and_next_write_succeeds(api):
    client, accounts, engine, _ = api
    headers = login(client, accounts["admin"])
    name = "Conflict-" + uuid4().hex
    assert (
        client.post(POSITIONS, headers=headers, json={"name": name}).status_code == 201
    )
    conflict = client.post(POSITIONS, headers=headers, json={"name": name})
    assert conflict.status_code == 409
    assert conflict.headers["content-type"] == "application/problem+json"
    assert conflict.json()["type"].endswith(":position-name-conflict")
    assert count(engine, name) == 1
    other = name + "-next"
    assert (
        client.post(POSITIONS, headers=headers, json={"name": other}).status_code == 201
    )
    assert count(engine, other) == 1


def test_denied_and_invalid_requests_do_not_write_rows(api):
    client, accounts, engine, _ = api
    name = "Denied-" + uuid4().hex
    unauthenticated = client.post(POSITIONS, json={"name": name})
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["www-authenticate"].lower() == "bearer"
    employee = login(client, accounts["employee"])
    denied = client.post(POSITIONS, headers=employee, json={"name": name})
    assert denied.status_code == 403
    assert denied.headers["content-type"] == "application/problem+json"
    assert count(engine, name) == 0
    admin = login(client, accounts["admin"])
    invalid = client.post(POSITIONS, headers=admin, json={"name": " "})
    assert invalid.status_code == 422
    assert invalid.headers["content-type"] == "application/problem+json"
    assert count(engine, " ") == 0


def test_deactivation_persists_and_keeps_the_unique_name(api):
    client, accounts, engine, _ = api
    headers = login(client, accounts["admin"])
    name = "Deactivate-" + uuid4().hex
    created = client.post(POSITIONS, headers=headers, json={"name": name})
    assert created.status_code == 201
    path = created.headers["location"]
    result = client.post(path + "/deactivation", headers=headers)
    assert result.status_code == 200
    assert result.json()["is_active"] is False
    with Session(engine) as session:
        position = session.get(Position, int(created.json()["id"]))
        assert position is not None and position.is_active is False
    assert client.get(path, headers=headers).json()["is_active"] is False
    assert (
        client.post(POSITIONS, headers=headers, json={"name": name}).status_code == 409
    )


def test_logout_revokes_database_session_and_blocks_later_writes(api):
    client, accounts, engine, _ = api
    headers = login(client, accounts["admin"])
    result = client.delete("/api/v1/auth/sessions/current", headers=headers)
    assert result.status_code == 204
    with Session(engine) as session:
        auth_session = session.scalar(
            select(AuthSession).where(AuthSession.user_id == accounts["admin"]["id"])
        )
        assert auth_session is not None and auth_session.revoked_at is not None
    name = "Revoked-" + uuid4().hex
    rejected = client.post(POSITIONS, headers=headers, json={"name": name})
    assert rejected.status_code == 401
    assert count(engine, name) == 0
