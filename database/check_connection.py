"""Check TLS, default grants and runtime restrictions using disposable data."""

import argparse
import sys
import uuid

import psycopg
from psycopg import sql

from bootstrap import read_connection


def check(vault_name):
    app_config = read_connection(vault_name, "app")
    migration_config = read_connection(vault_name, "migration")
    for config, expected_user in [(app_config, "dreamplanner_app"), (migration_config, "dreamplanner_migrator")]:
        if (config.get("user") != expected_user or config.get("dbname") != "dreamplanner"
                or config.get("sslmode") != "verify-full"):
            raise ValueError("Unexpected connection configuration")
    if app_config.get("host") != migration_config.get("host"):
        raise ValueError("Connections must target the same host")
    table = sql.Identifier("public", "_connection_check_" + uuid.uuid4().hex)
    created = False
    with psycopg.connect(**migration_config, connect_timeout=15, autocommit=True) as migration:
        try:
            migration.execute(sql.SQL("CREATE TABLE {} (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, value TEXT NOT NULL)").format(table))
            created = True
            with psycopg.connect(**app_config, connect_timeout=15, autocommit=True) as app:
                tls = app.execute("SELECT ssl, version FROM pg_stat_ssl WHERE pid = pg_backend_pid()").fetchone()
                if not tls or not tls[0]:
                    raise AssertionError("TLS not active")
                print(f"PASS: certificate/hostname verification and {tls[1]}")
                key = app.execute(sql.SQL("INSERT INTO {} (value) VALUES (%s) RETURNING id").format(table), ("demo",)).fetchone()[0]
                assert app.execute(sql.SQL("SELECT value FROM {} WHERE id = %s").format(table), (key,)).fetchone()[0] == "demo"
                app.execute(sql.SQL("UPDATE {} SET value = %s WHERE id = %s").format(table), ("updated", key))
                assert app.execute(sql.SQL("SELECT value FROM {} WHERE id = %s").format(table), (key,)).fetchone()[0] == "updated"
                app.execute(sql.SQL("DELETE FROM {} WHERE id = %s").format(table), (key,))
                assert app.execute(sql.SQL("SELECT count(*) FROM {}").format(table)).fetchone()[0] == 0
                print("PASS: runtime CRUD and identity sequence on a table created by the migration login")
                permissions = app.execute(
                    "SELECT has_schema_privilege(current_user, 'public', 'CREATE'), "
                    "has_database_privilege(current_user, current_database(), 'CREATE'), "
                    "rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user"
                ).fetchone()
                assert not any(permissions), "Runtime login has elevated privileges"
                try:
                    with app.transaction():
                        app.execute(sql.SQL("ALTER TABLE {} ADD COLUMN forbidden INTEGER").format(table))
                        raise AssertionError("Runtime login unexpectedly altered the table")
                except psycopg.errors.InsufficientPrivilege:
                    pass
                print("PASS: runtime cannot alter tables, create schemas or administer SQL roles")
        finally:
            if created:
                migration.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(table))
    print("Disposable verification table removed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-name", required=True)
    args = parser.parse_args()
    try:
        check(args.vault_name)
    except (psycopg.Error, RuntimeError, ValueError, OSError, AssertionError) as error:
        print(f"Connection check failed: {type(error).__name__}; SQLSTATE={getattr(error, 'sqlstate', None)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
