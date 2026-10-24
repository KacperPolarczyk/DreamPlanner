#!/bin/sh
set -eu

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v ON_ERROR_STOP=1 \
  -v app_db="$POSTGRES_DB" \
  -v test_db="$POSTGRES_TEST_DB" \
  -v app_password="$ISOLATED_APP_PASSWORD" \
  -v migration_password="$ISOLATED_MIGRATION_PASSWORD" \
  -v test_app_password="$ISOLATED_TEST_APP_PASSWORD" \
  -v test_migration_password="$ISOLATED_TEST_MIGRATION_PASSWORD" <<'SQL'
CREATE ROLE dreamplanner_isolated_app LOGIN PASSWORD :'app_password';
CREATE ROLE dreamplanner_isolated_migration LOGIN PASSWORD :'migration_password';
CREATE ROLE dreamplanner_app LOGIN PASSWORD :'test_app_password';
CREATE ROLE dreamplanner_migration LOGIN PASSWORD :'test_migration_password';

REVOKE CONNECT ON DATABASE :"app_db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"app_db"
  TO dreamplanner_isolated_app, dreamplanner_isolated_migration;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO dreamplanner_isolated_app;
GRANT USAGE, CREATE ON SCHEMA public TO dreamplanner_isolated_migration;
ALTER DEFAULT PRIVILEGES FOR ROLE dreamplanner_isolated_migration IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dreamplanner_isolated_app;
ALTER DEFAULT PRIVILEGES FOR ROLE dreamplanner_isolated_migration IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO dreamplanner_isolated_app;

CREATE DATABASE :"test_db";
REVOKE CONNECT ON DATABASE :"test_db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"test_db" TO dreamplanner_app, dreamplanner_migration;
SQL

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_TEST_DB" \
  -v ON_ERROR_STOP=1 <<'SQL'
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO dreamplanner_app;
GRANT USAGE, CREATE ON SCHEMA public TO dreamplanner_migration;
ALTER DEFAULT PRIVILEGES FOR ROLE dreamplanner_migration IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dreamplanner_app;
ALTER DEFAULT PRIVILEGES FOR ROLE dreamplanner_migration IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO dreamplanner_app;
SQL
