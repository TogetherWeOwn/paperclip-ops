#!/usr/bin/env bash
# Independent host-side pg_dump fallback; deployment/host authorization is separate.
# flock serializes this producer. The unchanged pg_stat_activity COPY query is a
# courtesy check, not a lock against the in-server scheduler (a race remains).
# Verify a private temporary stream before atomic, no-clobber publication. Prune
# only top-level paperclip-*-hostcron.sql.gz files older than the existing policy.
# A published archive and a completed retention cycle are separate facts.
# Canonical receipt/verification contract: docs/host-db-backup.md; --help is offline.
set -uo pipefail

ME="${BASH_SOURCE[0]##*/}"
CONTAINER="${PAPERCLIP_DB_CTR:-paperclip-db}"
BACKUP_DIR="${PAPERCLIP_DB_BACKUP_DIR:-/paperclip/instances/default/data/backups}"
RETENTION_DAYS="${PAPERCLIP_DB_BACKUP_RETENTION_DAYS:-2}"
LOCK_DIR="${HOST_DB_BACKUP_LOCK_DIR:-/tmp}"
LOCK_FILE="$LOCK_DIR/host_db_backup.lock"
TAG="hostcron"
DRY_RUN=0
JSON=0
HELP=0
CYCLE_ID="${EPOCHREALTIME//./}-${BASHPID}-${RANDOM}"
ARCHIVE_ID=null
BACKUP_LANDED=false
RETENTION_STATUS=not_started
PRUNED=null
PARTIAL=""
LIST=""

# JSON strings are fixed tokens or generated safe identifiers, never dump data,
# database/user identities, caller-supplied paths, or raw external diagnostics.
cleanup() {
  local rc=0 path
  for path in "$PARTIAL" "$LIST"; do
    [[ -z "$path" ]] || rm -f -- "$path" 2>/dev/null || rc=1
  done
  PARTIAL=""; LIST=""
  return "$rc"
}

finish() {
  local rc="$1" outcome="$2" stage="$3" code="$4" retryable="$5" action="$6" message="$7"
  if ! cleanup; then
    rc=1; outcome=error; stage=cleanup; code=temporary_cleanup_failed
    retryable=false; action=inspect_owned_temporary_artifact
    message="ERROR: owned temporary artifact cleanup failed"
  fi
  if [[ "$BACKUP_LANDED" == true && "$outcome" == error ]]; then outcome=partial; fi
  if (( JSON )); then
    [[ "$code" == null ]] || code="\"$code\""
    printf '{"schema_version":1,"cycle_id":"%s","archive_id":%s,"outcome":"%s","stage":"%s","error_code":%s,"retryable":%s,"next_action":"%s","backup_landed":%s,"retention":{"status":"%s","pruned":%s}}\n' \
      "$CYCLE_ID" "$ARCHIVE_ID" "$outcome" "$stage" "$code" "$retryable" "$action" "$BACKUP_LANDED" "$RETENTION_STATUS" "$PRUNED"
  elif (( rc != 0 )) || [[ "$outcome" == skipped ]]; then
    printf '%s\n' "$message" >&2
  else
    printf '%s\n' "$message"
  fi
  exit "$rc"
}

refuse() { finish 2 error arguments invalid_arguments false correct_arguments "$1"; }

usage() {
  printf '%s\n' "usage: $ME [--json] [--dry-run] [--backup-dir DIR] [--container NAME] [--retention-days N]" '
Independent host-side pg_dump fallback; execution requires existing authorization.
--dry-run: no-write plan, before locks/artifacts or any podman/DB call.
--json: one v1 receipt on stdout, including on argument errors; no DB/user values.
Fields: schema_version, cycle_id, archive_id (null until publication), outcome,
stage, error_code, retryable, next_action, backup_landed, retention {status, pruned}.
Outcomes: success, error, partial, planned, skipped. Unknown pruned count is null.
Exit: 0 complete/planned/skipped; 1 dump/verify/publish/retention error; 2 refused.
A partial cycle can have a valid published archive; it must not be redumped blindly.
Defaults/env: PAPERCLIP_DB_CTR=paperclip-db; PAPERCLIP_DB_BACKUP_RETENTION_DAYS=2;
PAPERCLIP_DB_BACKUP_DIR=the existing instance backup directory;
HOST_DB_BACKUP_LOCK_DIR=/tmp. No scheduler, restore or new execution grant.
Discover: ./host_db_backup.sh --help
Plan:     ./host_db_backup.sh --json --dry-run
Details and stable stage/error tokens: docs/host-db-backup.md'
}

# Recognize JSON even when a preceding flag is invalid. Parsing never runs tools.
for arg in "$@"; do [[ "$arg" != --json ]] || JSON=1; done
while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) JSON=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --backup-dir|--container|--retention-days)
      flag="$1"; shift
      [[ $# -gt 0 && -n "$1" && "$1" != -* ]] || refuse "ERROR: $flag needs a value"
      case "$flag" in
        --backup-dir) BACKUP_DIR="$1" ;;
        --container) CONTAINER="$1" ;;
        --retention-days) RETENTION_DAYS="$1" ;;
      esac ;;
    -h|--help) HELP=1 ;;
    *) refuse "ERROR: unknown argument" ;;
  esac
  shift
done
[[ "$RETENTION_DAYS" =~ ^[1-9][0-9]*$ ]] || refuse "ERROR: --retention-days must be a positive integer"
[[ "$CONTAINER" =~ ^[A-Za-z0-9._-]+$ ]] || refuse "ERROR: invalid container name"
if (( HELP )); then usage; exit 0; fi
if (( DRY_RUN )); then
  finish 0 planned plan null false obtain_execution_authorization 'DRY-RUN: would lock, check concurrent COPY, dump, verify, publish without overwrite, then prune only aged hostcron archives; no live state checked'
fi

for tool in podman gzip flock date find stat mktemp mv rm awk; do
  command -v "$tool" >/dev/null 2>&1 || finish 2 error precondition missing_tool false install_required_tools "ERROR: $tool is required"
done
[[ -d "$BACKUP_DIR" && -w "$BACKUP_DIR" ]] || finish 2 error precondition invalid_backup_directory false correct_backup_directory 'ERROR: backup directory must exist and be writable'
# Absolute paths keep every pathname operand unambiguous to downstream tools.
[[ "$BACKUP_DIR" == /* ]] || BACKUP_DIR="$PWD/$BACKUP_DIR"

trap cleanup EXIT
trap 'finish 1 error interrupted interrupted false inspect_cycle_before_retry "ERROR: cycle interrupted"' HUP INT TERM
exec 9>"$LOCK_FILE" || finish 2 error lock lock_open_failed false correct_lock_directory 'ERROR: cannot open lock file'
if ! flock -n 9; then
  finish 0 skipped lock null true wait_for_active_cycle "SKIP: another $ME cycle already holds the lock; not overlapping it"
fi
podman container exists "$CONTAINER" 2>/dev/null || finish 2 error precondition container_unavailable false inspect_container 'ERROR: container is absent or unavailable'

# Keep the existing courtesy query and fail-open behavior for an unmeasurable
# count. This signal does not prove that the in-server scheduler has recovered.
active_dumps="$(podman exec "$CONTAINER" psql -X -A -t -U "$(podman exec "$CONTAINER" sh -c 'echo "$POSTGRES_USER"' 2>/dev/null)" \
  -d "$(podman exec "$CONTAINER" sh -c 'echo "$POSTGRES_DB"' 2>/dev/null)" \
  -c "SELECT count(*) FROM pg_stat_activity WHERE query LIKE 'COPY %TO stdout%' AND pid <> pg_backend_pid();" 2>/dev/null)"
if [[ "$active_dumps" =~ ^[0-9]+$ ]] && (( active_dumps > 0 )); then
  finish 0 skipped courtesy null true wait_for_active_dump 'SKIP: another COPY-to-stdout backend is running; not stacking a second dump'
fi
PG_USER="$(podman exec "$CONTAINER" sh -c 'echo "$POSTGRES_USER"' 2>/dev/null)" \
  || finish 2 error precondition database_identity_unavailable false inspect_container_configuration 'ERROR: could not read database user'
PG_DB="$(podman exec "$CONTAINER" sh -c 'echo "$POSTGRES_DB"' 2>/dev/null)" \
  || finish 2 error precondition database_identity_unavailable false inspect_container_configuration 'ERROR: could not read database name'
[[ -n "$PG_USER" && -n "$PG_DB" ]] \
  || finish 2 error precondition database_identity_unavailable false inspect_container_configuration 'ERROR: database identity is empty'

TS="$(date -u +%Y%m%d-%H%M%S)" \
  || finish 2 error precondition clock_failed false inspect_clock 'ERROR: cannot create archive timestamp'
[[ "$TS" =~ ^[0-9]{8}-[0-9]{6}$ ]] \
  || finish 2 error precondition clock_failed false inspect_clock 'ERROR: invalid archive timestamp'
FINAL="$BACKUP_DIR/paperclip-${TS}-${TAG}.sql.gz"
[[ ! -e "$FINAL" && ! -L "$FINAL" ]] \
  || finish 1 error publication destination_exists false inspect_existing_archive 'ERROR: archive destination already exists; not overwriting it'
# mktemp creates exclusively; never reuse or unlink someone else's .partial.
PARTIAL="$(mktemp "$BACKUP_DIR/.paperclip-${TS}-${TAG}.XXXXXXXX.partial")" \
  || finish 1 error dump temporary_create_failed false inspect_backup_directory 'ERROR: cannot create owned temporary archive'
if ! podman exec "$CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" --no-owner --no-privileges 2>/dev/null \
    | gzip -c > "$PARTIAL"; then
  finish 1 error dump dump_pipeline_failed true inspect_dump_failure 'ERROR: pg_dump | gzip pipeline failed'
fi

size="$(stat -c %s "$PARTIAL" 2>/dev/null)" \
  || finish 1 error verification archive_stat_failed false inspect_archive_verification 'ERROR: cannot measure temporary archive'
[[ "$size" =~ ^[0-9]+$ ]] \
  || finish 1 error verification archive_stat_failed false inspect_archive_verification 'ERROR: invalid archive size'
if (( size < 1024 )); then
  finish 1 error verification archive_too_small false inspect_dump_failure "ERROR: dump is only $size bytes, under the 1024-byte floor"
fi
if ! gzip -t -- "$PARTIAL" 2>/dev/null; then
  finish 1 error verification gzip_integrity_failed false inspect_dump_failure 'ERROR: gzip integrity check failed'
fi

# Stream through a bounded-state verifier, never SQL execution. COPY rows are
# data, even if they look exactly like a completion marker or CREATE TABLE.
# Accept the legacy bare terminal marker and the standard comment block, plus
# a matching modern pg_dump unrestrict after the footer when restrict is used.
create_count="$(gzip -cd -- "$PARTIAL" 2>/dev/null | awk '
  BEGIN { marker="-- PostgreSQL database dump complete" }
  {
    if (copy) { if ($0 == "\\.") copy=0; next }
    if ($0 ~ /^[[:space:]]*$/) next
    if (footer) {
      if (footer == 1 && $0 == "--") { footer=2; next }
      if (footer <= 2 && restrict_key != "" && $0 == "\\unrestrict " restrict_key) { footer=3; next }
      footer=0
    }
    if ($0 ~ /^\\restrict [A-Za-z0-9_]+$/) restrict_key=substr($0, 11)
    if ($0 == marker) footer=1
    if ($0 ~ /^CREATE TABLE /) tables++
    if ($0 ~ /^COPY .* FROM stdin;$/) copy=1
  }
  END {
    if (copy || !footer || (restrict_key != "" && footer != 3)) { print "invalid_trailer"; exit 1 }
    if (tables < 1) { print "missing_tables"; exit 1 }
    print tables
  }')"; verify_rc=$?
if (( verify_rc != 0 )); then
  if [[ "$create_count" == missing_tables ]]; then
    finish 1 error verification missing_create_table false inspect_dump_failure 'ERROR: dump contains no CREATE TABLE statements'
  fi
  finish 1 error verification invalid_terminal_trailer false inspect_dump_failure 'ERROR: dump has no valid terminal completion trailer'
fi
[[ "$create_count" =~ ^[1-9][0-9]*$ ]] \
  || finish 1 error verification invalid_terminal_trailer false inspect_dump_failure 'ERROR: unmeasurable dump structure'

# GNU mv -nT does not replace an existing entry or move into a directory. Some
# versions return 0 on a no-clobber skip: a surviving source is NOT publication.
if ! mv -nT -- "$PARTIAL" "$FINAL" 2>/dev/null; then
  finish 1 error publication atomic_publication_failed false inspect_destination_before_retry 'ERROR: atomic archive publication failed'
fi
if [[ -e "$PARTIAL" || -L "$PARTIAL" || ! -f "$FINAL" || -L "$FINAL" ]]; then
  finish 1 error publication destination_exists false inspect_existing_archive 'ERROR: archive was not published; destination may already exist'
fi
PARTIAL=""
BACKUP_LANDED=true
ARCHIVE_ID="\"paperclip-${TS}-${TAG}.sql.gz\""
if (( ! JSON )); then
  printf 'host_db_backup=PASS file=%s size_bytes=%s tables=%s\n' "${FINAL##*/}" "$size" "$create_count"
fi

# Capture traversal AND its exit status before deleting anything. Process
# substitution hides find failures; an incomplete listing is never zero pruned.
RETENTION_STATUS=failed
LIST="$(mktemp "$BACKUP_DIR/.host-db-retention.XXXXXXXX")" \
  || finish 1 partial retention retention_list_failed false inspect_retention_without_redump 'ERROR: partial cycle; backup landed but retention listing could not be created'
if ! find "$BACKUP_DIR" -maxdepth 1 -type f -name "paperclip-*-${TAG}.sql.gz" -mtime "+${RETENTION_DAYS}" -print0 > "$LIST" 2>/dev/null; then
  finish 1 partial retention retention_enumeration_failed false inspect_retention_without_redump 'ERROR: partial cycle; backup landed but retention traversal failed; pruned=unknown'
fi
PRUNED=0
retention_failed=0
while IFS= read -r -d '' old; do
  [[ "$old" != "$FINAL" ]] || continue
  if rm -f -- "$old" 2>/dev/null; then
    PRUNED=$((PRUNED + 1))
  else
    retention_failed=1
  fi
done < "$LIST"
if (( retention_failed )); then
  finish 1 partial retention retention_deletion_failed false inspect_retention_without_redump "ERROR: partial cycle; backup landed but retention deletion failed; pruned=$PRUNED"
fi
RETENTION_STATUS=complete
finish 0 success complete null false none "host_db_backup_retention: pruned=$PRUNED retention_days=$RETENTION_DAYS"
