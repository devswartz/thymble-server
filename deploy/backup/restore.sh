#!/usr/bin/env bash
# Restore a Thymble backup made by thymble-backup.
#
#   thymble-restore --verify FILE   restore into a scratch database, print row counts, drop
#                                   it. Safe to run any time; production is untouched.
#   thymble-restore --yes FILE      REPLACE the live database (PGDATABASE) with FILE, then
#                                   reset PowerSync's storage so it re-replicates.
#
# FILE is a path, or a name inside BACKUP_DIR (/backups). Encrypted dumps (*.age) need the
# age identity (private key): BACKUP_AGE_IDENTITY_FILE, mounted into the container only for
# the restore. Stop api, worker and powersync before a real restore (see docs/self-hosting.md).
set -Eeuo pipefail
umask 077

BACKUP_DIR="${BACKUP_DIR:-/backups}"
PGDATABASE="${PGDATABASE:-thymble}"
export PGDATABASE
POWERSYNC_STORAGE_DB="${POWERSYNC_STORAGE_DB:-powersync_storage}"
POWERSYNC_ROLE="${POWERSYNC_ROLE:-powersync_role}"

log() { printf '%s restore: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() {
	log "$*"
	exit 1
}
usage() {
	sed -n '2,10p' "$0" >&2
	exit 2
}

mode=""
file=""
for arg in "$@"; do
	case "$arg" in
	--verify) mode=verify ;;
	--yes) mode=restore ;;
	-h | --help) usage ;;
	-*) usage ;;
	*) file="$arg" ;;
	esac
done
[ -n "$mode" ] && [ -n "$file" ] || usage
[ -f "$file" ] || file="$BACKUP_DIR/$file"
[ -f "$file" ] || die "no such backup: $file"

work=$(mktemp -d)
scratch=""
cleanup() {
	rm -rf "$work"
	if [ -n "$scratch" ]; then dropdb --if-exists --force "$scratch" 2>/dev/null || true; fi
}
trap cleanup EXIT

# Checksum (when the .sha256 written next to the dump is there).
if [ -f "$file.sha256" ]; then
	(cd "$(dirname "$file")" && sha256sum --quiet -c "$(basename "$file").sha256") ||
		die "checksum mismatch for $file"
fi

dump="$file"
case "$file" in
*.age)
	identity="${BACKUP_AGE_IDENTITY_FILE:-}"
	[ -n "$identity" ] && [ -f "$identity" ] ||
		die "$file is encrypted: mount the age identity and set BACKUP_AGE_IDENTITY_FILE"
	dump="$work/restore.dump"
	age -d -i "$identity" -o "$dump" "$file"
	;;
esac
pg_restore --list "$dump" >/dev/null || die "$file is not a readable pg_dump archive"

psql_q() { psql -X -v ON_ERROR_STOP=1 -qtA "$@"; }

counts() { # counts DB
	psql_q -d "$1" <<'SQL'
SELECT 'alembic_version: ' || coalesce((SELECT string_agg(version_num, ',') FROM alembic_version), '-');
SELECT format('%s: %s rows', c.relname,
              (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM %I', c.relname), false, true, '')))[1]::text)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname <> 'alembic_version'
ORDER BY c.relname;
SQL
}

if [ "$mode" = verify ]; then
	scratch="thymble_restore_check_$$"
	log "restoring $(basename "$file") into scratch database $scratch"
	createdb "$scratch"
	pg_restore --exit-on-error --no-owner --no-privileges --dbname="$scratch" "$dump"
	counts "$scratch"
	log "verify OK: $(basename "$file") restores cleanly"
	exit 0
fi

# --- real restore ------------------------------------------------------------------------
log "REPLACING database $PGDATABASE on ${PGHOST:-localhost} with $(basename "$file")"
active=$(psql_q -d postgres -c "SELECT count(*) FROM pg_stat_activity
    WHERE datname IN ('$PGDATABASE', '$POWERSYNC_STORAGE_DB') AND pid <> pg_backend_pid()
    AND backend_type = 'client backend'")
if [ "$active" -gt 0 ]; then
	log "$active connection(s) still open; stop api, worker and powersync first. Continuing:"
	log "they will be disconnected."
fi

# PowerSync's logical replication slots pin the old database; drop them first.
psql_q -d postgres -c "SELECT pg_terminate_backend(active_pid) FROM pg_replication_slots
    WHERE database = '$PGDATABASE' AND active_pid IS NOT NULL" >/dev/null
psql_q -d postgres -c "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots
    WHERE database = '$PGDATABASE'" >/dev/null

dropdb --force --if-exists "$PGDATABASE"
createdb --owner="$PGUSER" "$PGDATABASE"
pg_restore --exit-on-error --no-owner --role="$PGUSER" --dbname="$PGDATABASE" "$dump"

# What deploy/postgres/init-powersync.sh sets up, made sure of after the restore
# (idempotent: the dump usually carries the grants and the publication already).
psql_q -d "$PGDATABASE" -v ps_role="$POWERSYNC_ROLE" <<'SQL'
GRANT USAGE ON SCHEMA public TO :"ps_role";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"ps_role";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO :"ps_role";
SELECT 'CREATE PUBLICATION powersync FOR ALL TABLES'
WHERE NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'powersync')
\gexec
SQL

# PowerSync's bucket storage describes the old database; start it from scratch so it
# re-replicates. Devices then download their data again.
dropdb --force --if-exists "$POWERSYNC_STORAGE_DB"
createdb --owner="$POWERSYNC_ROLE" "$POWERSYNC_STORAGE_DB"

counts "$PGDATABASE"
log "restore complete. Start the stack again; migrations run on start (alembic upgrade head)."
