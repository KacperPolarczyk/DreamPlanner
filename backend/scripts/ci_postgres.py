"""Run each PostgreSQL integration module against its own disposable local database."""

import os
import re
import secrets
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import psycopg
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import URL

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
MODULES = sorted((ROOT / "tests/integration").glob("test_*.py"))
NAMES = {
    "test_assignments_postgres": "dreamplanner_test_assignments_125",
    "test_schedules_postgres": "dreamplanner_test_schedules_98",
    "test_availability_postgres": "dreamplanner_test_availability_117",
    "test_shift_requirements_postgres": "dreamplanner_test_shift_requirements_107",
    "test_publication_races_postgres": "dreamplanner_test_publication_races_134",
}
REQUIRED = set(NAMES) | {
    "test_domain_migration",
    "test_auth_migration",
    "test_postgres_connection",
}


def main() -> int:
    """Create least-privilege roles and fail on any skipped integration test."""
    assert REQUIRED <= {module.stem for module in MODULES}
    assert os.getenv("CI") == "true", "This runner is restricted to CI"
    admin_url = os.environ["CI_POSTGRES_ADMIN_URL"]
    parsed_admin = psycopg.conninfo.conninfo_to_dict(admin_url)
    assert parsed_admin.get("host") in {"localhost", "127.0.0.1"}
    port = int(parsed_admin.get("port", "5432"))
    app_password = secrets.token_urlsafe(32)
    migration_password = secrets.token_urlsafe(32)
    totals = {key: 0 for key in ("passed", "failed", "skipped", "errors")}
    failed = False
    with psycopg.connect(admin_url, autocommit=True) as connection:
        for role, password in (
            ("dreamplanner_app", app_password),
            ("dreamplanner_migration", migration_password),
        ):
            connection.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )

        base_name = "dreamplanner_ci_connection"
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(base_name))
        )
        connection.execute(
            sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(base_name)
            )
        )
        connection.execute(
            sql.SQL(
                "GRANT CONNECT ON DATABASE {} "
                "TO dreamplanner_app, dreamplanner_migration"
            ).format(sql.Identifier(base_name))
        )
        with psycopg.connect(
            psycopg.conninfo.make_conninfo(admin_url, dbname=base_name), autocommit=True
        ) as database:
            database.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            database.execute("GRANT USAGE ON SCHEMA public TO dreamplanner_app")
            database.execute(
                "GRANT USAGE, CREATE ON SCHEMA public TO dreamplanner_migration"
            )

        def db_url(role: str, password: str, name: str) -> str:
            return URL.create(
                "postgresql+psycopg",
                username=role,
                password=password,
                host="127.0.0.1",
                port=port,
                database=name,
            ).render_as_string(hide_password=False)

        base_app_url = db_url("dreamplanner_app", app_password, base_name)
        base_migration_url = db_url(
            "dreamplanner_migration", migration_password, base_name
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            for module in MODULES:
                name = NAMES.get(
                    module.stem,
                    f"dreamplanner_test_{module.stem.removeprefix('test_')}",
                )
                assert re.fullmatch(r"dreamplanner_test_[a-z0-9_]+", name)
                connection.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
                )
                connection.execute(
                    sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                        sql.Identifier(name)
                    )
                )
                connection.execute(
                    sql.SQL(
                        "GRANT CONNECT ON DATABASE {} "
                        "TO dreamplanner_app, dreamplanner_migration"
                    ).format(sql.Identifier(name))
                )
                db_admin = psycopg.conninfo.make_conninfo(admin_url, dbname=name)
                with psycopg.connect(db_admin, autocommit=True) as database:
                    database.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
                    database.execute("GRANT USAGE ON SCHEMA public TO dreamplanner_app")
                    database.execute(
                        "GRANT USAGE, CREATE ON SCHEMA public TO dreamplanner_migration"
                    )
                    database.execute(
                        "ALTER DEFAULT PRIVILEGES FOR ROLE dreamplanner_migration "
                        "IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE "
                        "ON TABLES TO dreamplanner_app"
                    )
                    database.execute(
                        "ALTER DEFAULT PRIVILEGES FOR ROLE dreamplanner_migration "
                        "IN SCHEMA public GRANT USAGE, SELECT "
                        "ON SEQUENCES TO dreamplanner_app"
                    )

                app_url = db_url("dreamplanner_app", app_password, name)
                migration_url = db_url(
                    "dreamplanner_migration", migration_password, name
                )
                environment = os.environ.copy()
                environment.update(
                    DATABASE_URL=base_app_url,
                    MIGRATION_DATABASE_URL=base_migration_url,
                    TEST_DATABASE_URL=app_url,
                    TEST_MIGRATION_DATABASE_URL=migration_url,
                    RUN_DB_INTEGRATION="1",
                )
                if module.stem == "test_publication_races_postgres":
                    old = os.environ.get("MIGRATION_DATABASE_URL")
                    os.environ["MIGRATION_DATABASE_URL"] = migration_url
                    try:
                        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
                    finally:
                        if old is None:
                            os.environ.pop("MIGRATION_DATABASE_URL", None)
                        else:
                            os.environ["MIGRATION_DATABASE_URL"] = old
                report = Path(temp_dir) / f"{module.stem}.xml"
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        "-q",
                        "--tb=line",
                        str(module),
                        f"--junitxml={report}",
                    ],
                    cwd=ROOT,
                    env=environment,
                    check=False,
                )
                root = ET.parse(report).getroot() if report.exists() else None
                suite = (
                    root.find("testsuite")
                    if root is not None and root.tag == "testsuites"
                    else root
                )
                counts = {key: 0 for key in totals}
                if suite is not None:
                    counts["failed"] = int(suite.get("failures", 0))
                    counts["errors"] = int(suite.get("errors", 0))
                    counts["skipped"] = int(suite.get("skipped", 0))
                    counts["passed"] = int(suite.get("tests", 0)) - sum(
                        counts[key] for key in ("failed", "errors", "skipped")
                    )
                if suite is None:
                    counts["errors"] = 1
                for key, value in counts.items():
                    totals[key] += value
                print(
                    f"{module.name}: "
                    + ", ".join(f"{key}={value}" for key, value in counts.items()),
                    flush=True,
                )
                if (
                    result.returncode
                    or counts["failed"]
                    or counts["errors"]
                    or counts["skipped"]
                    or not counts["passed"]
                ):
                    print(
                        "Integration suite failed or skipped required tests",
                        file=sys.stderr,
                    )
                    failed = True
    print(
        "PostgreSQL integration total: "
        + ", ".join(f"{key}={value}" for key, value in totals.items())
    )
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
