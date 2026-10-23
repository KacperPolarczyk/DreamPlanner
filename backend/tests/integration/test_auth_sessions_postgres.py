"""Authentication session lifecycle on an isolated PostgreSQL database."""

import base64
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Annotated
from uuid import uuid4

import pytest
from alembic.config import Config
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from src.auth.dependencies import require_roles
from src.auth.jwt_codec import JwtCodec
from src.auth.models import AccountActionToken, AuthSession, RefreshToken
from src.auth.passwords import hash_password
from src.auth.session_repository import AuthSessionRepository
from src.auth.session_service import (
    ActiveIdentity,
    InvalidCredentials,
    InvalidSession,
    login,
    refresh_session,
    revoke_session,
    validate_session,
)
from src.auth.tokens import generate_action_token, hash_action_token, hash_refresh_token
from src.core.config import Settings
from src.core.database import get_session
from src.main import create_app
from src.users.models import Role, User
from src.users.password_service import InvalidCurrentPassword, change_own_password

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("TEST_MIGRATION_DATABASE_URL")
        or not os.getenv("TEST_DATABASE_URL"),
        reason="Set both TEST_*_DATABASE_URL values for a disposable local database",
    ),
]


def _check_test_urls(migration_url: str, app_url: str) -> None:
    """Refuse any nonlocal, non-disposable or development database."""
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


def _add_user(factory: sessionmaker[Session], email: str, active: bool) -> int:
    """Create a test account directly after applying the real migration."""
    with factory() as session:
        role = session.scalar(select(Role).where(Role.code == "employee"))
        assert role is not None
        user = User(
            role_id=role.id,
            email=email,
            full_name="Test User",
            password_hash=hash_password("correct-password"),
            is_active=active,
            activated_at=datetime.now(UTC) if active else None,
        )
        session.add(user)
        session.flush()
        user_id = user.id
        session.commit()
    return user_id


def test_session_lifecycle_on_disposable_postgres(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cover login, rotation, reuse, concurrency, expiry and rollback."""
    migration_url = os.environ["TEST_MIGRATION_DATABASE_URL"]
    app_url = os.environ["TEST_DATABASE_URL"]
    _check_test_urls(migration_url, app_url)
    migration_engine = create_engine(migration_url)
    app_engine = create_engine(app_url)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    monkeypatch.setenv("MIGRATION_DATABASE_URL", migration_url)
    upgraded = False
    try:
        with migration_engine.connect() as connection:
            assert not inspect(connection).has_table("users")
        command.upgrade(config, "head")
        upgraded = True
        factory = sessionmaker(bind=app_engine, autoflush=False, autocommit=False)
        codec = JwtCodec(
            Settings(
                jwt_signing_key_base64=base64.b64encode(
                    secrets.token_bytes(32)
                ).decode(),
                jwt_current_kid="test-v1",
                jwt_issuer="dreamplanner-test",
                jwt_audience="dreamplanner-test-api",
            )
        )
        user_id = _add_user(factory, "active@example.com", True)
        inactive_id = _add_user(factory, "inactive@example.com", False)

        for email, password in (
            ("unknown@example.com", "correct-password"),
            ("active@example.com", "wrong-password"),
            ("inactive@example.com", "correct-password"),
        ):
            with factory() as session:
                with pytest.raises(InvalidCredentials, match="Invalid credentials"):
                    login(session, email, password, codec)
                assert not session.in_transaction()

        with factory() as session:
            first = login(session, "ACTIVE@example.com", "correct-password", codec)
        with factory() as session:
            second_session = login(
                session, "active@example.com", "correct-password", codec
            )
        assert first.user_id == second_session.user_id == user_id
        assert first.session_id != second_session.session_id
        with factory() as session:
            identity = validate_session(session, first.session_id, user_id)
            assert identity.role == "employee"
            assert session.in_transaction()
        with factory() as session:
            stored = session.get(AuthSession, first.session_id)
            assert stored is not None
            assert stored.idle_expires_at - stored.created_at == timedelta(hours=24)
            assert stored.absolute_expires_at - stored.created_at == timedelta(days=7)
            token = session.scalar(
                select(RefreshToken).where(RefreshToken.session_id == first.session_id)
            )
            assert token is not None
            assert token.token_hash == hash_refresh_token(first.refresh_token)
            assert first.refresh_token.encode() != token.token_hash

        with factory() as session:
            with pytest.raises(InvalidSession):
                refresh_session(session, "unknown-refresh-token", codec)
        with factory() as session:
            assert session.get(AuthSession, first.session_id).revoked_at is None

        with factory() as session:
            successor = refresh_session(session, first.refresh_token, codec)
        assert successor.refresh_token != first.refresh_token
        with factory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(RefreshToken)
                    .where(
                        RefreshToken.session_id == first.session_id,
                        RefreshToken.used_at.is_(None),
                    )
                )
                == 1
            )

        with factory() as session:
            with pytest.raises(InvalidSession):
                refresh_session(session, first.refresh_token, codec)
        with factory() as session:
            assert session.get(AuthSession, first.session_id).revoked_at is not None
            with pytest.raises(InvalidSession):
                refresh_session(session, successor.refresh_token, codec)
        with factory() as session:
            with pytest.raises(InvalidSession):
                validate_session(session, first.session_id, user_id)
        with factory() as session:
            surviving = refresh_session(session, second_session.refresh_token, codec)

        with factory() as session:
            absolute = login(session, "active@example.com", "correct-password", codec)
        with factory() as session:
            row = session.get(AuthSession, absolute.session_id)
            row.created_at = datetime.now(UTC) - timedelta(days=8)
            row.idle_expires_at = datetime.now(UTC) - timedelta(days=1)
            row.absolute_expires_at = datetime.now(UTC) - timedelta(days=1)
            session.commit()
        with factory() as session:
            with pytest.raises(InvalidSession):
                refresh_session(session, absolute.refresh_token, codec)

        with factory() as session:
            expired = session.get(AuthSession, surviving.session_id)
            expired.created_at = datetime.now(UTC) - timedelta(days=2)
            expired.idle_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()
        with factory() as session:
            with pytest.raises(InvalidSession):
                refresh_session(session, surviving.refresh_token, codec)

        with factory() as session:
            capped = login(session, "active@example.com", "correct-password", codec)
        with factory() as session:
            row = session.get(AuthSession, capped.session_id)
            row.created_at = datetime.now(UTC) - timedelta(days=6)
            row.absolute_expires_at = datetime.now(UTC) + timedelta(hours=1)
            row.idle_expires_at = row.absolute_expires_at
            session.commit()
        with factory() as session:
            refresh_session(session, capped.refresh_token, codec)
        with factory() as session:
            row = session.get(AuthSession, capped.session_id)
            assert row.idle_expires_at == row.absolute_expires_at

        with factory() as session:
            race = login(session, "active@example.com", "correct-password", codec)
        barrier = Barrier(2)

        def concurrent_refresh() -> str:
            barrier.wait(timeout=5)
            with factory() as session:
                try:
                    refresh_session(session, race.refresh_token, codec)
                except InvalidSession:
                    return "rejected"
                return "rotated"

        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(lambda _: concurrent_refresh(), range(2)))
        assert sorted(outcomes) == ["rejected", "rotated"]
        with factory() as session:
            assert session.get(AuthSession, race.session_id).revoked_at is not None
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(RefreshToken)
                    .where(
                        RefreshToken.session_id == race.session_id,
                        RefreshToken.used_at.is_(None),
                    )
                )
                == 1
            )

        with factory() as session:
            logout = login(session, "active@example.com", "correct-password", codec)
        with factory() as session:
            revoke_session(session, logout.session_id, user_id)
        with factory() as session:
            assert session.get(AuthSession, logout.session_id).revoked_at is not None
            with pytest.raises(InvalidSession):
                refresh_session(session, logout.refresh_token, codec)

        with factory() as session:
            rollback = login(session, "active@example.com", "correct-password", codec)
        with monkeypatch.context() as patch:

            def fail_after_consumption(
                repository: AuthSessionRepository, token: RefreshToken
            ) -> None:
                raise RuntimeError("successor insertion failed")

            patch.setattr(AuthSessionRepository, "add_token", fail_after_consumption)
            with factory() as session:
                with pytest.raises(RuntimeError, match="successor insertion failed"):
                    refresh_session(session, rollback.refresh_token, codec)
                assert not session.in_transaction()
        with factory() as session:
            token = session.scalar(
                select(RefreshToken).where(
                    RefreshToken.token_hash
                    == hash_refresh_token(rollback.refresh_token)
                )
            )
            assert token is not None and token.used_at is None
            assert session.get(AuthSession, rollback.session_id).revoked_at is None

        with factory() as session:
            before = session.scalar(
                select(func.count())
                .select_from(AuthSession)
                .where(AuthSession.user_id == user_id)
            )
        with monkeypatch.context() as patch:
            patch.setattr(
                codec,
                "issue",
                lambda user_id, session_id: (_ for _ in ()).throw(
                    RuntimeError("signing failed")
                ),
            )
            with factory() as session:
                with pytest.raises(RuntimeError, match="signing failed"):
                    login(session, "active@example.com", "correct-password", codec)
                assert not session.in_transaction()
        with factory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AuthSession)
                    .where(AuthSession.user_id == user_id)
                )
                == before
            )

        monkeypatch.setenv(
            "JWT_SIGNING_KEY_BASE64",
            base64.b64encode(codec.keys[codec.current_kid]).decode(),
        )
        monkeypatch.setenv("JWT_CURRENT_KID", codec.current_kid)
        monkeypatch.setenv("JWT_ISSUER", codec.issuer)
        monkeypatch.setenv("JWT_AUDIENCE", codec.audience)
        rbac_app = create_app()

        def rbac_session():
            with factory() as session:
                yield session

        rbac_app.dependency_overrides[get_session] = rbac_session

        @rbac_app.get("/test/rbac/manager")
        def manager_only(
            identity: Annotated[ActiveIdentity, Depends(require_roles("manager"))],
        ) -> dict[str, str]:
            return {"role": identity.role}

        headers = {"Authorization": f"Bearer {rollback.access_token}"}
        with TestClient(rbac_app) as rbac_client:
            assert (
                rbac_client.get("/test/rbac/manager", headers=headers).status_code
                == 403
            )
        with factory() as session:
            manager = session.scalar(select(Role).where(Role.code == "manager"))
            assert manager is not None
            user = session.get(User, user_id)
            user.role_id = manager.id
            session.commit()
        with TestClient(rbac_app) as rbac_client:
            updated_role = rbac_client.get("/test/rbac/manager", headers=headers)
            assert updated_role.status_code == 200
            assert updated_role.json() == {"role": "manager"}
        with factory() as session:
            assert (
                validate_session(session, rollback.session_id, user_id).role
                == "manager"
            )
        with factory() as session:
            user = session.get(User, user_id)
            user.is_active = False
            session.commit()
        with TestClient(rbac_app) as rbac_client:
            inactive_account = rbac_client.get("/test/rbac/manager", headers=headers)
            assert inactive_account.status_code == 401
            assert inactive_account.headers["www-authenticate"] == "Bearer"
        with factory() as session:
            with pytest.raises(InvalidSession):
                validate_session(session, rollback.session_id, user_id)

        activation = generate_action_token()
        now = datetime.now(UTC)
        with factory() as session:
            session.add(
                AccountActionToken(
                    id=uuid4(),
                    user_id=inactive_id,
                    purpose="activation",
                    token_hash=hash_action_token(activation),
                    issued_at=now,
                    expires_at=now + timedelta(hours=24),
                )
            )
            session.commit()
        monkeypatch.setenv(
            "JWT_SIGNING_KEY_BASE64",
            base64.b64encode(secrets.token_bytes(32)).decode(),
        )
        monkeypatch.setenv("JWT_CURRENT_KID", "http-test-v1")
        monkeypatch.setenv("JWT_ISSUER", "dreamplanner-http-test")
        monkeypatch.setenv("JWT_AUDIENCE", "dreamplanner-http-api")
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("REFRESH_COOKIE_SECURE", "true")
        monkeypatch.setenv("CORS_ORIGINS", '["https://app.example.com"]')
        app = create_app()

        def db_session():
            with factory() as session:
                yield session

        app.dependency_overrides[get_session] = db_session
        browser_headers = {"Origin": "https://app.example.com"}
        with TestClient(
            app,
            base_url="https://api.example.com",
            client=("198.51.100.12", 40000),
        ) as client:
            invalid_activation = client.post(
                "/api/v1/auth/activations",
                headers=browser_headers,
                json={"token": "invalid", "new_password": "chosen-password"},
            )
            assert invalid_activation.status_code == 401
            activation_response = client.post(
                "/api/v1/auth/activations",
                headers=browser_headers,
                json={"token": activation, "new_password": "chosen-password"},
            )
            assert activation_response.status_code == 204
            assert activation_response.text == ""

            missing_origin = client.post(
                "/api/v1/auth/sessions",
                json={"email": "inactive@example.com", "password": "chosen-password"},
            )
            assert missing_origin.status_code == 403
            missing_account = client.post(
                "/api/v1/auth/sessions",
                headers=browser_headers,
                json={"email": "missing@example.com", "password": "wrong"},
            )
            wrong_password = client.post(
                "/api/v1/auth/sessions",
                headers=browser_headers,
                json={"email": "inactive@example.com", "password": "wrong"},
            )
            assert missing_account.status_code == wrong_password.status_code == 401
            assert missing_account.json() == wrong_password.json()
            assert "set-cookie" not in missing_account.headers
            login_response = client.post(
                "/api/v1/auth/sessions",
                headers=browser_headers,
                json={"email": "inactive@example.com", "password": "chosen-password"},
            )
            assert login_response.status_code == 200
            assert set(login_response.json()) == {
                "access_token",
                "token_type",
                "expires_in",
            }
            assert login_response.json()["expires_in"] == 600
            assert login_response.headers["cache-control"] == "no-store"
            cookie = login_response.headers["set-cookie"]
            for attribute in (
                "HttpOnly",
                "Secure",
                "SameSite=lax",
                "Path=/api/v1/auth/sessions/current",
            ):
                assert attribute in cookie
            old_refresh = client.cookies.get("dreamplanner_refresh")
            assert old_refresh and old_refresh not in login_response.text
            access = login_response.json()["access_token"]
            assert app.state.jwt_codec.verify(access)[0] == inactive_id

            refreshed = client.post(
                "/api/v1/auth/sessions/current/refresh", headers=browser_headers
            )
            assert refreshed.status_code == 200
            new_refresh = client.cookies.get("dreamplanner_refresh")
            assert new_refresh and new_refresh != old_refresh
            assert new_refresh not in refreshed.text
            assert refreshed.json()["access_token"] != access

            with TestClient(app, base_url="https://api.example.com") as replay:
                reused = replay.post(
                    "/api/v1/auth/sessions/current/refresh",
                    headers={
                        **browser_headers,
                        "Cookie": f"dreamplanner_refresh={old_refresh}",
                    },
                )
            assert reused.status_code == 401
            assert reused.json()["type"] == "urn:dreamplanner:problem:invalid-session"
            after_reuse = client.post(
                "/api/v1/auth/sessions/current/refresh", headers=browser_headers
            )
            assert after_reuse.status_code == 401

            second_login = client.post(
                "/api/v1/auth/sessions",
                headers=browser_headers,
                json={"email": "inactive@example.com", "password": "chosen-password"},
            )
            assert second_login.status_code == 200
            second_access = second_login.json()["access_token"]
            altered_access = second_access[:-2] + "xx"
            rejected_jwt = client.delete(
                "/api/v1/auth/sessions/current",
                headers={
                    **browser_headers,
                    "Authorization": f"Bearer {altered_access}",
                },
            )
            assert rejected_jwt.status_code == 401
            assert rejected_jwt.headers["www-authenticate"] == "Bearer"
            logout = client.delete(
                "/api/v1/auth/sessions/current",
                headers={**browser_headers, "Authorization": f"Bearer {second_access}"},
            )
            assert logout.status_code == 204
            assert "Path=/api/v1/auth/sessions/current" in logout.headers["set-cookie"]
            assert "Max-Age=0" in logout.headers["set-cookie"]
            assert client.cookies.get("dreamplanner_refresh") is None
            repeated_logout = client.delete(
                "/api/v1/auth/sessions/current",
                headers={**browser_headers, "Authorization": f"Bearer {second_access}"},
            )
            assert repeated_logout.status_code == 401
            assert repeated_logout.headers["www-authenticate"] == "Bearer"

        with factory() as session:
            password_grant = login(
                session, "inactive@example.com", "chosen-password", codec
            )
        with factory() as session:
            account = session.get(User, inactive_id)
            original_created_at = account.created_at
            original_updated_at = account.updated_at
        with factory() as session:
            with pytest.raises(InvalidCurrentPassword):
                change_own_password(
                    session,
                    inactive_id,
                    password_grant.session_id,
                    "wrong-password",
                    "replacement-password",
                )
        with factory() as session:
            assert session.get(User, inactive_id).updated_at == original_updated_at
        with factory() as session:
            change_own_password(
                session,
                inactive_id,
                password_grant.session_id,
                "chosen-password",
                "replacement-password",
            )
        with factory() as session:
            account = session.get(User, inactive_id)
            assert account.created_at == original_created_at
            assert account.updated_at > original_updated_at
            assert account.updated_at.utcoffset() == timedelta(0)
            assert session.get(AuthSession, password_grant.session_id).revoked_at

        captured = capsys.readouterr()
        for secret in (activation, old_refresh, new_refresh, "chosen-password"):
            assert secret not in captured.out + captured.err + caplog.text
    finally:
        if upgraded:
            command.downgrade(config, "base")
        app_engine.dispose()
        migration_engine.dispose()
