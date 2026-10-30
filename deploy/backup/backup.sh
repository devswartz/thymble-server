#!/usr/bin/env bash
# Thymble database backups: pg_dump -Fc, optional age encryption, optional off-site copy.
#
#   thymble-backup schedule        run `run` every day at BACKUP_AT (the service's default)
#   thymble-backup run             take one backup now
#   thymble-backup list            list local backups
#   thymble-backup verify [FILE]   restore FILE (default: newest) into a scratch database,
#                                  print row counts, drop it again. Production is untouched.
#   thymble-backup restore ...     see thymble-restore (replaces the live database)
#   thymble-backup health          container healthcheck: last success within 26 hours
#
# Connection: the standard libpq variables PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE.
# Configuration (all optional):
#   BACKUP_DIR=/backups              where dumps are written
#   BACKUP_AT=03:30                  daily time, in the container's TZ
#   BACKUP_KEEP_DAYS=14              local retention
#   BACKUP_AGE_RECIPIENTS=           age public keys (space/comma separated); encrypts dumps
#   BACKUP_REQUIRE_ENCRYPTION=false  refuse to write unencrypted dumps
#   BACKUP_RCLONE_REMOTE=            rclone destination, e.g. "offsite:thymble-backups"
#   BACKUP_OFFSITE_KEEP_DAYS=30      off-site retention (0 = keep everything)
#   BACKUP_OFFSITE_ALLOW_UNENCRYPTED=false
#   BACKUP_NOTIFY_URL=               POSTed JSON {"status","message","host","file"} on failure
#   BACKUP_NOTIFY_ON_SUCCESS=false   ...and on success too
#   BACKUP_VERIFY=false              restore every dump into a scratch database first
set -Eeuo pipefail
umask 077

BACKUP_DIR="${BACKUP_DIR:-/backups}"
BACKUP_AT="${BACKUP_AT:-03:30}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
BACKUP_OFFSITE_KEEP_DAYS="${BACKUP_OFFSITE_KEEP_DAYS:-30}"
PGDATABASE="${PGDATABASE:-thymble}"
export PGDATABASE
LAST_SUCCESS="$BACKUP_DIR/.last-success"
STARTED=/tmp/.backup-started

log() { printf '%s backup: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
truthy() { case "${1:-}" in 1 | true | TRUE | True | yes | on) return 0 ;; *) return 1 ;; esac; }

json_escape() {
	local s=${1//\\/\\\\}
	s=${s//\"/\\\"}
	s=${s//$'\n'/ }
	printf '%s' "$s"
}

notify() { # notify STATUS MESSAGE [FILE]
	[ -n "${BACKUP_NOTIFY_URL:-}" ] || return 0
	local body
	body=$(printf '{"status":"%s","message":"%s","host":"%s","file":"%s"}' \
		"$1" "$(json_escape "$2")" "$(json_escape "$(hostname)")" "$(json_escape "${3:-}")")
	curl -fsS -m 20 -X POST -H 'Content-Type: application/json' -d "$body" \
		"$BACKUP_NOTIFY_URL" >/dev/null || log "notification to BACKUP_NOTIFY_URL failed"
}

recipients() { # one age recipient per line
	tr ', ' '\n\n' <<<"${BACKUP_AGE_RECIPIENTS:-}" | sed '/^$/d'
}

newest() {
	find "$BACKUP_DIR" -maxdepth 1 -type f -name 'thymble-*.dump*' ! -name '*.sha256' \
		-printf '%T@ %p\n' | sort -rn | awk 'NR == 1 { sub(/^[^ ]+ /, ""); print }'
}

do_run() {
	mkdir -p "$BACKUP_DIR"
	local stamp name partial final
	stamp=$(date -u +%Y%m%dT%H%M%SZ)
	name="thymble-${stamp}.dump"
	partial="$BACKUP_DIR/.partial-${name}"
	rm -f "$BACKUP_DIR"/.partial-* # leftovers from an interrupted run

	log "dumping database $PGDATABASE on ${PGHOST:-localhost}"
	pg_dump --format=custom --compress=6 --no-password --file="$partial" "$PGDATABASE"
	# The archive must at least be readable before it replaces anything.
	pg_restore --list "$partial" >/dev/null
	if truthy "${BACKUP_VERIFY:-false}"; then
		# Prove the plain dump restores before it is encrypted (no private key needed here).
		thymble-restore --verify "$partial"
	fi

	if [ -n "$(recipients)" ]; then
		local args=()
		while IFS= read -r r; do args+=(-r "$r"); done < <(recipients)
		age "${args[@]}" -o "$partial.age" "$partial"
		rm -f "$partial"
		final="$BACKUP_DIR/${name}.age"
		mv "$partial.age" "$final"
	elif truthy "${BACKUP_REQUIRE_ENCRYPTION:-false}"; then
		log "BACKUP_REQUIRE_ENCRYPTION is set but BACKUP_AGE_RECIPIENTS is empty"
		return 1
	else
		final="$BACKUP_DIR/${name}"
		mv "$partial" "$final"
	fi
	(cd "$BACKUP_DIR" && sha256sum "$(basename "$final")" >"$(basename "$final").sha256")
	log "wrote $(basename "$final") ($(du -h "$final" | cut -f1))"

	if [ -n "${BACKUP_RCLONE_REMOTE:-}" ]; then
		case "$final" in
		*.age) ;;
		*) if ! truthy "${BACKUP_OFFSITE_ALLOW_UNENCRYPTED:-false}"; then
			log "refusing to copy an unencrypted dump off-site (set BACKUP_AGE_RECIPIENTS)"
			return 1
		fi ;;
		esac
		log "copying to $BACKUP_RCLONE_REMOTE"
		rclone copy --no-traverse "$final" "$BACKUP_RCLONE_REMOTE"
		rclone copy --no-traverse "$final.sha256" "$BACKUP_RCLONE_REMOTE"
		if [ "$BACKUP_OFFSITE_KEEP_DAYS" -gt 0 ]; then
			rclone delete --min-age "${BACKUP_OFFSITE_KEEP_DAYS}d" --include 'thymble-*.dump*' \
				"$BACKUP_RCLONE_REMOTE"
		fi
	fi

	find "$BACKUP_DIR" -maxdepth 1 -type f -name 'thymble-*.dump*' -mtime +"$BACKUP_KEEP_DAYS" \
		-print -delete | sed 's/^/pruned /' >&2

	touch "$LAST_SUCCESS"
	log "backup complete"
	if truthy "${BACKUP_NOTIFY_ON_SUCCESS:-false}"; then
		notify ok "Thymble backup complete" "$(basename "$final")"
	fi
}

run_reporting() {
	local out status=0
	out=$(mktemp)
	# A separate process, so `set -e` applies inside the run (bash ignores it in functions
	# called from a `||` context).
	"$0" _run 2> >(tee "$out" >&2) || status=$?
	sleep 0.2 # let tee flush
	if [ "$status" -ne 0 ]; then
		log "backup FAILED (exit $status)"
		notify failed "Thymble backup failed: $(tail -n 5 "$out")"
	fi
	rm -f "$out"
	return "$status"
}

seconds_until_next() {
	local now target
	now=$(date +%s)
	target=$(date -d "today $BACKUP_AT" +%s)
	if [ "$target" -le "$now" ]; then target=$(date -d "tomorrow $BACKUP_AT" +%s); fi
	echo $((target - now))
}

do_schedule() {
	date -d "today $BACKUP_AT" >/dev/null 2>&1 || {
		log "BACKUP_AT must be HH:MM, got '$BACKUP_AT'"
		exit 2
	}
	touch "$STARTED"
	mkdir -p "$BACKUP_DIR"
	log "scheduled daily at $BACKUP_AT ($(date +%Z)); keeping ${BACKUP_KEEP_DAYS} days in $BACKUP_DIR"
	if [ -n "$(recipients)" ]; then
		log "encrypting for $(recipients | wc -l) age recipient(s)"
	fi
	if [ -n "${BACKUP_RCLONE_REMOTE:-}" ]; then
		log "off-site copy to $BACKUP_RCLONE_REMOTE"
	fi
	trap 'log "stopping"; exit 0' TERM INT
	while true; do
		local secs
		secs=$(seconds_until_next)
		log "next backup in ${secs}s"
		sleep "$secs" &
		wait $!
		run_reporting || true
	done
}

do_health() {
	# Healthy if the last backup succeeded within 26 hours, or the service started less
	# than 26 hours ago (no backup due yet).
	if [ -f "$LAST_SUCCESS" ] && [ -n "$(find "$LAST_SUCCESS" -mmin -1560)" ]; then exit 0; fi
	if [ -f "$STARTED" ] && [ -n "$(find "$STARTED" -mmin -1560)" ]; then exit 0; fi
	exit 1
}

do_list() {
	find "$BACKUP_DIR" -maxdepth 1 -type f -name 'thymble-*.dump*' ! -name '*.sha256' \
		-printf '%TY-%Tm-%Td %TH:%TM  %10s  %f\n' | sort
}

cmd="${1:-schedule}"
[ $# -gt 0 ] && shift
case "$cmd" in
schedule) do_schedule ;;
run) run_reporting ;;
_run) do_run ;;
list) do_list ;;
health) do_health ;;
verify)
	file="${1:-$(newest)}"
	[ -n "$file" ] || {
		log "no backups in $BACKUP_DIR"
		exit 1
	}
	exec thymble-restore --verify "$file"
	;;
restore) exec thymble-restore "$@" ;;
*)
	sed -n '2,12p' "$0" >&2
	exit 2
	;;
esac
