#!/bin/sh
set -eu

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v ON_ERROR_STOP=1 \
  -v database_name="$POSTGRES_DB" \
  -v app_password="$POSTGRES_APP_PASSWORD" \
  -v migration_password="$POSTGRES_MIGRATION_PASSWORD" <<'SQL'
CREATE ROLE dreamplanner_app LOGIN PASSWORD :'app_password';
CREATE ROLE dreamplanner_migration LOGIN PASSWORD :'migration_password';

REVOKE CONNECT ON DATABASE :"database_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database_name" TO dreamplanner_app, dreamplanner_migration;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO dreamplanner_app;
GRANT USAGE, CREATE ON SCHEMA public TO dreamplanner_migration;

ALTER DEFAULT PRIVILEGES FOR ROLE dreamplanner_migration IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dreamplanner_app;
ALTER DEFAULT PRIVILEGES FOR ROLE dreamplanner_migration IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO dreamplanner_app;
SQL
