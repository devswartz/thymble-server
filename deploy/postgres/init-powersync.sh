#!/bin/sh
# Runs once, when the Postgres data volume is first created.
# Sets up what PowerSync needs: a replication role, a publication, and a separate
# database for PowerSync's own bucket storage.
set -eu

: "${POWERSYNC_DB_PASSWORD:?POWERSYNC_DB_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v ps_password="$POWERSYNC_DB_PASSWORD" <<'SQL'
CREATE ROLE powersync_role WITH LOGIN REPLICATION PASSWORD :'ps_password';

-- Read access to the app schema, including tables created later by migrations.
GRANT USAGE ON SCHEMA public TO powersync_role;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO powersync_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO powersync_role;

-- PowerSync replicates from this publication (name is fixed by PowerSync).
CREATE PUBLICATION powersync FOR ALL TABLES;

-- PowerSync's bucket storage lives in its own database.
CREATE DATABASE powersync_storage OWNER powersync_role;
SQL
