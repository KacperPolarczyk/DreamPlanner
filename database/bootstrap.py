"""Create restricted SQL logins using connection secrets from Azure Key Vault.

Run as the DBA after Terraform apply. Does not install application tables;
the backend owns schema changes through Alembic using the migration login.
"""

import argparse
import json
import subprocess
import sys

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict


def read_connection(vault_name, role):
    result = subprocess.run(
        ["az", "keyvault", "secret", "show", "--vault-name", vault_name,
         "--name", f"database-url-{role}", "--query", "value", "-o", "json"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Cannot read the {role} connection secret; check Azure login and Key Vault RBAC")
    return conninfo_to_dict(json.loads(result.stdout))


def bootstrap(vault_name):
    connections = {role: read_connection(vault_name, role) for role in ("admin", "migration", "app")}
    admin = connections["admin"]
    expected = {"admin": "dreamplanner_admin", "migration": "dreamplanner_migrator", "app": "dreamplanner_app"}
    for role, config in connections.items():
        if (config.get("user") != expected[role]
                or config.get("host") != admin.get("host")
                or config.get("dbname") != "dreamplanner"
                or config.get("sslmode") != "verify-full"
                or not config.get("password")):
            raise ValueError("Unexpected connection configuration; refusing SQL privilege changes")

    with psycopg.connect(**admin, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            for role in ("migration", "app"):
                name = expected[role]
                existing = cur.execute(
                    "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls "
                    "FROM pg_roles WHERE rolname = %s", (name,),
                ).fetchone()
                if existing is None:
                    cur.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(name)))
                elif any(existing):
                    raise ValueError("Existing SQL login has elevated privileges; manual review required")
                if cur.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_auth_members m "
                    "JOIN pg_roles r ON r.oid = m.member WHERE r.rolname = %s)", (name,),
                ).fetchone()[0]:
                    raise ValueError("SQL login inherits another role; manual review required")
                cur.execute(sql.SQL(
                    "ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(name), sql.Literal(connections[role]["password"])))

            # The DBA must be able to administer objects owned by the migration login.
            # PostgreSQL already gives the creator ADMIN; granting ADMIN back to
            # its own grantor is rejected. SET is sufficient for ownership work.
            cur.execute("GRANT dreamplanner_migrator TO dreamplanner_admin WITH SET TRUE")
            cur.execute("REVOKE ALL ON DATABASE dreamplanner FROM PUBLIC")
            cur.execute("GRANT CONNECT, CREATE, TEMPORARY ON DATABASE dreamplanner TO dreamplanner_migrator")
            cur.execute("GRANT CONNECT ON DATABASE dreamplanner TO dreamplanner_app")
            cur.execute("ALTER SCHEMA public OWNER TO dreamplanner_migrator")
            cur.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            cur.execute("GRANT USAGE ON SCHEMA public TO dreamplanner_app")
            cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO dreamplanner_app")
            cur.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dreamplanner_app")
            cur.execute(
                "ALTER DEFAULT PRIVILEGES FOR ROLE dreamplanner_migrator IN SCHEMA public "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dreamplanner_app"
            )
            cur.execute(
                "ALTER DEFAULT PRIVILEGES FOR ROLE dreamplanner_migrator IN SCHEMA public "
                "GRANT USAGE, SELECT ON SEQUENCES TO dreamplanner_app"
            )
    print("SQL logins and grants configured. Application tables remain managed by Alembic.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-name", required=True)
    args = parser.parse_args()
    try:
        bootstrap(args.vault_name)
    except (psycopg.Error, RuntimeError, ValueError, OSError) as error:
        # Never log connection URIs, passwords, or rendered SQL containing passwords.
        print(f"Bootstrap failed: {type(error).__name__}; SQLSTATE={getattr(error, 'sqlstate', None)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
