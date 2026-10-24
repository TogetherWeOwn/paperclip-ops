#!/usr/bin/env bash
# ===========================================================================
# db_backup_stall.sh — detect a WEDGED database backup scheduler (case_14da0d3f).
# ---------------------------------------------------------------------------
# On 2026-08-30T09:37:42Z a scheduled backup started and never settled. It did
# not throw -- `grep -c "database backup failed" server.log` is 0 for the whole
# log -- so the `finally` at server/src/index.ts:722 never ran and
# `databaseBackupInFlight` stayed true for 92 hours. Every subsequent hourly
# tick no-opped at index.ts:671 with one WARN line. 90 backups were lost. An
# unrelated process restart at 2026-09-03T05:17:03Z cured it by accident.
#
# THE REASON THIS SCRIPT EXISTS: /api/health could not see any of it, and then
# erased the evidence. inspectDatabaseBackupHealth() only stats .sql.gz mtimes
# (services/database-backup-health.ts:81-103), so:
#
#   * for the first 26 hours the stall is INVISIBLE -- age is under maxAgeHours;
#   * after 26 hours it warns `database_backup_stale`, but that same warning is
#     produced by a merely-late backup, so it does not identify the cause;
#   * the instant ONE backup lands, status flips back to "ok" with warnings: []
#     and the 90 lost backups leave no trace in the endpoint at all.
#
# The leading indicator is the WARN line itself. It fires within ONE hour, it is
# unambiguous, and nothing surfaces it. This script surfaces it.
#
# Two independent signals, either of which is a stall:
#
#   1. SUPPRESSION -- a "Skipping scheduled database backup" WARN that is newer
#      than the newest "database backup complete". Backups are being actively
#      skipped right now. This is the definitive signal.
#   2. ORPHAN -- a `<prefix>-*.sql` older than --orphan-min-age-minutes whose
#      `.sql.gz` sibling is missing OR TRUNCATED. The JavaScript backup engine
#      writes the plain .sql first and gzips at the end
#      (packages/db/src/backup-lib.ts:962-968), so a stale .sql with no usable
#      .gz is a backup that died mid-write. NOTE this is corroborating, not
#      conclusive on its own: a backup legitimately in progress also has one,
#      which is what the minimum age screens out.
#
#      THE TRUNCATED CASE IS NOT HYPOTHETICAL -- it is the 2026-09-05 shape and
#      the original 2026-09-03 detector MISSED it. Measured on the host: the
#      .sql.gz mtime (06:36:44) PRECEDES the .sql mtime (06:37:02), so the gzip
#      output file is created when the dump OPENS, not when it succeeds. The
#      COPY loop then hung -- the 832 MB .sql ends mid-COPY with no COMMIT and
#      no "PostgreSQL database dump complete" -- and nothing was ever written
#      through the gzip stream, leaving a 20-byte empty frame.
#
#      That ordering is what broke the original rule. "An unpaired .sql is the
#      death certificate" assumed the .gz appears only on success; in fact it
#      appears immediately, so the wedged case has a COMPLETE-LOOKING pair and
#      scored exit 0 on a scheduler that had been dead for 11 hours. Existence
#      is not success: the .gz must be at least --min-backup-bytes to count.
#
#   3. STALENESS -- the newest usable in-server `.sql.gz` in the backup
#      directory is older than --max-backup-age-hours. Host fallback archives
#      (`*-hostcron.sql.gz`) are measured separately and excluded by default so
#      they cannot make a wedged in-server scheduler look healthy. This is the
#      signal of last resort and the only one that survives losing the log. It
#      exists because signals 1 and 2
#      both read sources that can silently go away: on 2026-09-05 the server
#      log stopped being written to at 05:33 (the process was replaced and the
#      new one logs elsewhere), so the suppression scan anchored on a
#      12-hour-old completion, found no skips after it, and reported health.
#      A detector whose log went stale must not be able to return 0.
#
# FAILING CLOSED IS THE WHOLE POINT. The log is multi-gigabyte, so this reads a
# bounded tail window (--window-bytes). If no "database backup complete" anchor
# is found inside that window, the run is INCONCLUSIVE (exit 2) and says so --
# it NEVER reports healthy from a window it could not anchor. A detector that
# goes green because it looked at too little of the log is the exact failure
# mode that let this incident run for 92 hours.
#
# The same rule now covers a log that is merely OLD. An anchor found in a file
# whose newest bytes predate the newest backup by more than --max-backup-age-
# hours is an anchor from a dead log, and a dead log cannot testify to present
# health -- that run is INCONCLUSIVE too, never green.
#
# This script is read-only: it stats the backup directory, reads the tail of the
# log, and optionally GETs /api/health. It never writes a backup, never deletes
# one, and never restarts anything.
#
#   ./db_backup_stall.sh                # human-readable
#   ./db_backup_stall.sh --json         # machine-readable
#
# --staleness-only (case_43a9cf5b): evaluate ONLY signal 3 (newest usable archive
# age) and skip signals 1/2 and the server-log entirely. This exists because
# by case_43a9cf5b signal 1 (log suppression) is a PERMANENT stall once the
# in-server scheduler wedges -- no code change and no file on disk clears
# databaseBackupInFlight (case_9b650c35) -- so a combined verdict can never go
# green again even after host_db_backup.sh (docs/host-db-backup.md) starts
# landing independently-verified archives on the same schedule. Since that
# script writes paperclip-<ts>-hostcron.sql.gz into the SAME directory, it
# already matches this detector's existing `$PREFIX-*.sql.gz` glob with no
# code change: --staleness-only answers "is there a recent archive usable by
# size/mtime at all" (not a restore proof), the availability question,
# regardless of which writer produced it or what the in-server scheduler's
# internal bookkeeping says.
#
# Exit status:
#   0  no stall detected (and the window was anchored)
#   1  STALL detected -- backups are being suppressed or died mid-write
#   2  inconclusive or tool failure -- log unreadable, or window unanchored
#
# --staleness-only exit status:
#   0  a usable archive exists within --max-backup-age-hours
#   1  every archive is stale, truncated, or absent
#   2  required directory/file freshness observations could not be measured
# ===========================================================================
set -uo pipefail

LOG_FILE="${PAPERCLIP_SERVER_LOG:-/paperclip/instances/default/logs/server.log}"
BACKUP_DIR="${PAPERCLIP_DB_BACKUP_DIR:-/paperclip/instances/default/data/backups}"
PREFIX="paperclip"
WINDOW_BYTES=$((256 * 1024 * 1024))
ORPHAN_MIN_AGE_MINUTES=90
JSON=0
CHECK_HEALTH=1
STALENESS_ONLY=0
# The host fallback writes `*-hostcron.sql.gz` into the same directory. Normal
# scheduler detection excludes that producer; --staleness-only deliberately
# includes every producer unless the caller explicitly requests the exclusion.
EXCLUDE_SUFFIX="hostcron"
EXCLUDE_ENABLED=1
EXCLUDE_WAS_EXPLICIT=0
# A gzip stream with no members is 20 bytes; a real dump here is ~160 MB. 1 KiB
# is far above the empty-frame size and far below any plausible real backup, so
# it separates the two without needing to know the database's size.
MIN_BACKUP_BYTES=1024
# Backups are hourly. Two hours is one missed tick plus slack -- it is the
# threshold the case_00b5a2b4 operator finding asked for, and it is well under the
# server's own 26-hour maxAgeHours, which is why /api/health stayed "ok".
MAX_BACKUP_AGE_HOURS=2

# Log markers. These are the exact strings server/src/index.ts logs; if the
# server rewords them this detector must fail closed rather than read silence
# as health, which is what the unanchored-window check below guarantees.
M_COMPLETE="database backup complete"
M_STARTING="database backup starting"
M_SKIP="Skipping scheduled database backup"
M_FAILED="database backup failed"

usage() {
  cat >&2 <<'EOF'
usage: db_backup_stall.sh [--json] [--log FILE] [--backup-dir DIR]
                          [--window-bytes N] [--orphan-min-age-minutes N]
                          [--min-backup-bytes N] [--max-backup-age-hours N]
                          [--prefix NAME] [--exclude-suffix NAME]
                          [--include-all-producers] [--no-health]
                          [--local-only | --dry-run] [--staleness-only]

Detects a wedged database backup scheduler from the server log's own
"Skipping scheduled database backup" WARN -- the leading indicator that
/api/health cannot see and later erases.

  0 no stall   1 STALL detected   2 inconclusive / tool failure

By default, host dumps/archives ending `-hostcron.sql` / `-hostcron.sql.gz`
are excluded from the in-server orphan/staleness signals and reported
separately. Change the producer marker with --exclude-suffix, or use
--include-all-producers to restore the legacy all-writers view.

Missing/unreadable directories, failed scans/stats and invalid/future mtimes
are unknown observations, not fresh backups. A measured empty directory is
a stall; unmeasured required evidence is inconclusive unless an independent
stall was positively observed. A valid independent fresh archive still counts.
JSON adds schemaVersion, signals, producers, codes and next_action to the
legacy fields. Unknown/excluded observations are explained in codes and notes.

--no-health, --local-only and its alias --dry-run observe local files only:
no network, board/state writes or credential config. They do not simulate a
restore or rearm a monitor. Without these flags, the optional GET /api/health
may use a temporary protected credential config; it is advisory, not files-only.

Local-only examples (paths are placeholders):
  ./db_backup_stall.sh --local-only --log ./current.log --backup-dir ./backups --json
  ./db_backup_stall.sh --dry-run --staleness-only --backup-dir ./backups --json

--staleness-only evaluates ONLY whether a usable archive exists within
--max-backup-age-hours (signal 3), ignoring the server log and the orphan
check entirely. It includes every producer by default, so an independent
writer (e.g. host_db_backup.sh, case_43a9cf5b) can satisfy that availability-only
check. Pass --exclude-suffix explicitly to narrow it to the in-server writer.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) JSON=1 ;;
    --no-health|--local-only|--dry-run) CHECK_HEALTH=0 ;;
    --staleness-only) STALENESS_ONLY=1 ;;
    --exclude-suffix) shift; [[ $# -gt 0 ]] || { echo "ERROR: --exclude-suffix needs a value" >&2; exit 2; }; EXCLUDE_SUFFIX="$1"; EXCLUDE_ENABLED=1; EXCLUDE_WAS_EXPLICIT=1 ;;
    --include-all-producers) EXCLUDE_ENABLED=0; EXCLUDE_WAS_EXPLICIT=1 ;;
    --log) shift; [[ $# -gt 0 ]] || { echo "ERROR: --log needs a value" >&2; exit 2; }; LOG_FILE="$1" ;;
    --backup-dir) shift; [[ $# -gt 0 ]] || { echo "ERROR: --backup-dir needs a value" >&2; exit 2; }; BACKUP_DIR="$1" ;;
    --prefix) shift; [[ $# -gt 0 ]] || { echo "ERROR: --prefix needs a value" >&2; exit 2; }; PREFIX="$1" ;;
    --window-bytes) shift; [[ $# -gt 0 ]] || { echo "ERROR: --window-bytes needs a value" >&2; exit 2; }; WINDOW_BYTES="$1" ;;
    --orphan-min-age-minutes) shift; [[ $# -gt 0 ]] || { echo "ERROR: --orphan-min-age-minutes needs a value" >&2; exit 2; }; ORPHAN_MIN_AGE_MINUTES="$1" ;;
    --min-backup-bytes) shift; [[ $# -gt 0 ]] || { echo "ERROR: --min-backup-bytes needs a value" >&2; exit 2; }; MIN_BACKUP_BYTES="$1" ;;
    --max-backup-age-hours) shift; [[ $# -gt 0 ]] || { echo "ERROR: --max-backup-age-hours needs a value" >&2; exit 2; }; MAX_BACKUP_AGE_HOURS="$1" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

# Match stat's bounded decimal domain before any arithmetic. Age options must
# also fit signed 64-bit seconds, so unit conversion cannot wrap a threshold.
for n in "$WINDOW_BYTES" "$ORPHAN_MIN_AGE_MINUTES" "$MIN_BACKUP_BYTES" "$MAX_BACKUP_AGE_HOURS"; do
  [[ "$n" =~ ^[1-9][0-9]{0,17}$ ]] || { echo "ERROR: numeric options must be positive integers of at most 18 digits" >&2; exit 2; }
done
(( ORPHAN_MIN_AGE_MINUTES <= 153722867280912930 && MAX_BACKUP_AGE_HOURS <= 2562047788015215 )) \
  || { echo "ERROR: numeric options exceed the signed 64-bit age range" >&2; exit 2; }
[[ "$PREFIX" =~ ^[A-Za-z0-9._-]+$ ]] \
  || { echo "ERROR: prefix contains characters that could escape the glob" >&2; exit 2; }
[[ -z "$EXCLUDE_SUFFIX" || "$EXCLUDE_SUFFIX" =~ ^[A-Za-z0-9._-]+$ ]] \
  || { echo "ERROR: exclude-suffix contains characters that could escape the glob" >&2; exit 2; }
for tool in tail grep stat date find; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: $tool is required" >&2; exit 2; }
done

# --staleness-only exists to answer "is there ANY usable archive" once the
# host fallback lands, so it defaults to counting every producer -- unless
# the caller explicitly asked to narrow it with --exclude-suffix (or widen
# it in default mode with --include-all-producers). Default (combined) mode
# always excludes the host producer: that mode's job is judging the
# IN-SERVER scheduler, and a host-side archive answering for it is exactly
# the case_076c1673 defect.
EFFECTIVE_EXCLUDE_ENABLED=$EXCLUDE_ENABLED
if (( STALENESS_ONLY == 1 && EXCLUDE_WAS_EXPLICIT == 0 )); then
  EFFECTIVE_EXCLUDE_ENABLED=0
fi

NOW_EPOCH="$(date -u +%s)"
NOW_ISO="$(date -u -d "@$NOW_EPOCH" +%Y-%m-%dT%H:%M:%SZ)"

# --- JSON emission ---------------------------------------------------------
# Values are emitted through a real escaper so a log line containing a quote or
# a backslash cannot produce malformed JSON that a consumer silently misparses.
json_escape() {
  local s="$1" code char escaped
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  # Filenames and log lines can contain any non-NUL control byte, not just
  # tabs/newlines. Escape them all without an external JSON dependency.
  for (( code=1; code<32; code++ )); do
    printf -v char '\\%03o' "$code"
    printf -v char '%b' "$char"
    printf -v escaped '\\u%04x' "$code"
    s="${s//"$char"/"$escaped"}"
  done
  printf '%s' "$s"
}

VERDICT="ok"
EXIT_CODE=0
REASONS=()
NOTES=()

add_reason() { REASONS+=("$1"); }
add_note()   { NOTES+=("$1"); }

# Stable IDs supplement (not replace) the legacy prose and fields. Unknown is
# an observation failure, never a fabricated zero-byte file or epoch-zero age.
declare -A SIGNAL_STATE=([suppression]=unknown [orphan]=unknown [freshness]=unknown)
declare -A SIGNAL_CODES=([suppression]="" [orphan]="" [freshness]="")
CODES=""
add_code() {
  local signal="$1" code="$2"
  [[ " ${SIGNAL_CODES[$signal]} " == *" $code "* ]] || SIGNAL_CODES[$signal]+=" $code"
  [[ " $CODES " == *" $code "* ]] || CODES+=" $code"
}

# Both scans classify the configured suffix identically, including custom tags.
select_producer() {
  local name="${1##*/}"
  name="${name%.gz}"
  PRODUCER_ID=in_server
  PRODUCER_SELECTED=1
  if [[ -n "$EXCLUDE_SUFFIX" && "$name" == *-"$EXCLUDE_SUFFIX".sql ]]; then
    PRODUCER_ID=host
    PRODUCER_SELECTED=$(( 1 - EFFECTIVE_EXCLUDE_ENABLED ))
  fi
}

# Keep numbers out of shell arithmetic until checked (also rejects overflow).
# GNU stat -L follows a regular-file symlink, just as the -f check does.
read_stat() {
  local field="$1" file="$2" signal="$3" code=stat_failed
  STAT_VALUE=""
  STAT_AGE_MIN=""
  if STAT_VALUE="$(stat -L -c "$field" -- "$file" 2>/dev/null)"; then
    if [[ ! -f "$file" ]]; then
      code=file_unmeasured
    elif [[ "$STAT_VALUE" =~ ^[0-9]{1,18}$ ]]; then
      STAT_VALUE=$(( 10#$STAT_VALUE ))
      [[ "$field" != %Y ]] && return 0
      # A writer may update a file after startup. Capture the clock AFTER stat
      # and reuse this observation's timestamp for validation, age and cutoff.
      # No tolerance: even one second beyond this clock is future evidence.
      if STAT_NOW_EPOCH="$(date -u +%s)" && [[ "$STAT_NOW_EPOCH" =~ ^[0-9]{1,18}$ ]]; then
        STAT_NOW_EPOCH=$(( 10#$STAT_NOW_EPOCH ))
        code=mtime_invalid
        if (( STAT_VALUE > 0 && STAT_VALUE <= STAT_NOW_EPOCH )); then
          STAT_AGE_MIN=$(( (STAT_NOW_EPOCH - STAT_VALUE) / 60 ))
          return 0
        fi
        (( STAT_VALUE > STAT_NOW_EPOCH )) && code=mtime_future
      else
        code=clock_unmeasured
      fi
    elif [[ "$field" == %Y ]]; then
      code=mtime_invalid
    fi
  fi
  add_code "$signal" "$code"
  add_note "UNKNOWN: ${file} (${PRODUCER_ID:-in_server}) has an unusable ${field} observation: ${code}; excluded from ${signal} evidence."
  return 1
}

json_codes() {
  local code comma=""
  printf '['
  for code in $1; do
    printf '%s"%s"' "$comma" "$code"
    comma=,
  done
  printf ']'
}

# --- Signal 1: suppression (the definitive leading indicator) ---------------
LOG_READABLE=0
WINDOW_ANCHORED=0
LAST_COMPLETE_LINE=""
LAST_SKIP_LINE=""
LAST_STARTING_LINE=""
SKIP_COUNT_IN_WINDOW=0
FAILED_COUNT_IN_WINDOW=0
LOG_SIZE_BYTES=0

if (( STALENESS_ONLY == 0 )) && [[ -r "$LOG_FILE" && -f "$LOG_FILE" ]]; then
  LOG_READABLE=1
  PRODUCER_ID=in_server
  if read_stat %s "$LOG_FILE" suppression; then
    LOG_SIZE_BYTES="$STAT_VALUE"
  else
    LOG_READABLE=0
  fi

  # Bounded tail. Ordering within the window is what matters, so record the
  # position of each marker's LAST occurrence rather than parsing timestamps --
  # the log's bracketed [HH:MM:SS] carries no date and would be ambiguous across
  # midnight, which is precisely the kind of subtle wrongness that turns a
  # detector into a false green.
  if ! WINDOW="$(tail -c "$WINDOW_BYTES" -- "$LOG_FILE" 2>/dev/null)"; then
    WINDOW=""
    LOG_READABLE=0
    add_code suppression log_read_failed
  fi

  if [[ -n "$WINDOW" ]]; then
    complete_pos="$(printf '%s\n' "$WINDOW" | grep -a -n -F -- "$M_COMPLETE" | tail -1 | cut -d: -f1)"
    skip_pos="$(printf '%s\n' "$WINDOW" | grep -a -n -F -- "$M_SKIP" | tail -1 | cut -d: -f1)"
    starting_pos="$(printf '%s\n' "$WINDOW" | grep -a -n -F -- "$M_STARTING" | tail -1 | cut -d: -f1)"

    LAST_COMPLETE_LINE="$(printf '%s\n' "$WINDOW" | grep -a -F -- "$M_COMPLETE" | tail -1)"
    LAST_SKIP_LINE="$(printf '%s\n' "$WINDOW" | grep -a -F -- "$M_SKIP" | tail -1)"
    LAST_STARTING_LINE="$(printf '%s\n' "$WINDOW" | grep -a -F -- "$M_STARTING" | tail -1)"
    SKIP_COUNT_IN_WINDOW="$(printf '%s\n' "$WINDOW" | grep -a -c -F -- "$M_SKIP")"
    FAILED_COUNT_IN_WINDOW="$(printf '%s\n' "$WINDOW" | grep -a -c -F -- "$M_FAILED")"

    # The anchor. Without a completed backup inside the window we cannot order
    # anything against it, so we refuse to draw a conclusion.
    if [[ -n "$complete_pos" ]]; then
      WINDOW_ANCHORED=1

      if [[ -n "$skip_pos" ]] && (( skip_pos > complete_pos )); then
        VERDICT="stall"
        add_code suppression backup_suppressed
        add_reason "SUPPRESSION: a '${M_SKIP}' WARN appears AFTER the most recent '${M_COMPLETE}' in the scanned window. The in-flight guard (server/src/index.ts:671) is wedged and every scheduled tick is a no-op."
      fi

      # A start with neither a completion nor a skip after it is the very first
      # hour of a wedge -- before the next tick has even fired.
      if [[ "$VERDICT" == "ok" && -n "$starting_pos" ]] && (( starting_pos > complete_pos )); then
        VERDICT="stall"
        add_code suppression backup_unsettled
        add_reason "UNSETTLED: the most recent '${M_STARTING}' has no matching '${M_COMPLETE}' after it. A backup is in flight and has not settled; if it never does, the guard wedges permanently."
      fi
    fi
  fi
fi

if (( STALENESS_ONLY == 1 )); then
  SIGNAL_STATE[suppression]=not_evaluated
  SIGNAL_STATE[orphan]=not_evaluated
  add_note "--staleness-only: the server log and the orphan/truncation signal were not evaluated. Only archive freshness was requested; see its measured/unknown state."
elif (( LOG_READABLE == 0 )); then
  add_code suppression log_unmeasured
  add_note "Server log is not readable at ${LOG_FILE} -- the suppression signal could not be evaluated."
elif (( WINDOW_ANCHORED == 0 )); then
  add_code suppression window_unanchored
  add_note "No '${M_COMPLETE}' anchor found in the last ${WINDOW_BYTES} bytes of ${LOG_FILE}. The window is UNANCHORED: this run cannot distinguish a healthy scheduler from a wedged one. Re-run with a larger --window-bytes."
fi

# --- Signal 2: orphaned or truncated in-progress dump -----------------------
# A .sql whose .gz sibling is MISSING or TRUNCATED. Treating "the sibling file
# exists" as "the backup succeeded" is what made this detector return exit 0 on
# the live 2026-09-05 wedge: the compress step had created a 20-byte .gz and
# then died. The sibling must be big enough to be a real archive.
#
# Skipped entirely in --staleness-only mode: an in-progress dump belonging to
# a DIFFERENT writer (e.g. host_db_backup.sh mid-run) is not this detector's
# concern once it is only answering "is there a recent usable archive."
ORPHANS=()
ORPHAN_AGES=()
ORPHAN_SIZES=()
ORPHAN_GZ_SIZES=()
BACKUP_DIR_READABLE=0
BACKUP_FILES=()
SCAN_DIR="$BACKUP_DIR"
[[ "$SCAN_DIR" == /* || "$SCAN_DIR" == ./* || "$SCAN_DIR" == ../* ]] || SCAN_DIR="./$SCAN_DIR"
# An empty caller expansion is missing evidence, not an alias for the CWD.
if [[ -n "$BACKUP_DIR" && -d "$SCAN_DIR" && -r "$SCAN_DIR" && -x "$SCAN_DIR" ]]; then
  # A glob silently looks empty when readdir fails. Read a NUL-delimited listing
  # AND its exit status, without a temp file or a pipeline that loses the status.
  mapfile -d '' -t BACKUP_FILES < <(
    find -H "$SCAN_DIR" -mindepth 1 -maxdepth 1 \( -name "$PREFIX-*.sql" -o -name "$PREFIX-*.sql.gz" \) -print0 2>/dev/null
    printf '%s\0' "$?"
  )
  scan_status="${BACKUP_FILES[-1]:-1}"
  unset 'BACKUP_FILES[-1]'
  [[ "$scan_status" == 0 ]] && BACKUP_DIR_READABLE=1
fi
if (( BACKUP_DIR_READABLE == 0 )); then
  add_code freshness directory_unmeasured
  (( STALENESS_ONLY == 0 )) && add_code orphan directory_unmeasured
  add_note "UNKNOWN: backup directory ${BACKUP_DIR} is absent, unreadable or its scan failed -- the orphan and staleness signals could not be fully measured."
fi
if (( STALENESS_ONLY == 0 )); then
  # Partial enumeration cannot clear health, but a file it did observe can
  # still establish a positive orphan finding after successful stat reads.
  (( BACKUP_DIR_READABLE == 1 )) && SIGNAL_STATE[orphan]=measured
  excluded_orphans=()
  for f in "${BACKUP_FILES[@]}"; do
    [[ "$f" == *.sql ]] || continue
    select_producer "$f"
    if (( PRODUCER_SELECTED == 0 )); then
      add_code orphan producer_excluded
      excluded_orphans+=("${f##*/}")
      continue
    fi
    if ! read_stat %Y "$f" orphan; then
      SIGNAL_STATE[orphan]=unknown
      continue
    fi
    mtime="$STAT_VALUE"
    age_min="$STAT_AGE_MIN"
    orphan_cutoff=$(( STAT_NOW_EPOCH - ORPHAN_MIN_AGE_MINUTES * 60 ))
    (( mtime > orphan_cutoff )) && continue
    if ! read_stat %s "$f" orphan; then
      SIGNAL_STATE[orphan]=unknown
      continue
    fi
    size="$STAT_VALUE"
    gz_size=-1
    if [[ -e "$f.gz" || -L "$f.gz" ]]; then
      if ! read_stat %s "$f.gz" orphan; then
        SIGNAL_STATE[orphan]=unknown
        continue
      fi
      gz_size="$STAT_VALUE"
      (( gz_size >= MIN_BACKUP_BYTES )) && continue
    fi
    ORPHANS+=("${f##*/}")
    ORPHAN_AGES+=("$age_min")
    ORPHAN_SIZES+=("$size")
    ORPHAN_GZ_SIZES+=("$gz_size")
  done
  if (( ${#excluded_orphans[@]} > 0 )); then
    add_note "EXCLUDED: ${#excluded_orphans[@]} files from producer host; the orphan signal uses --exclude-suffix ${EXCLUDE_SUFFIX}: ${excluded_orphans[*]}"
  fi
fi

if (( ${#ORPHANS[@]} > 0 )); then
  for i in "${!ORPHANS[@]}"; do
    oname="${ORPHANS[$i]}"
    oage="${ORPHAN_AGES[$i]}"
    osize="${ORPHAN_SIZES[$i]}"
    ogz="${ORPHAN_GZ_SIZES[$i]}"
    if (( ogz < 0 )); then
      add_code orphan orphan_dump
      add_reason "ORPHAN: ${oname} is ${oage} minutes old with no .sql.gz sibling (${osize} bytes). The JavaScript backup engine compresses only after writer.close() (packages/db/src/backup-lib.ts:1019-1025), so this dump died mid-write."
    else
      add_code orphan truncated_sibling
      add_reason "TRUNCATED: ${oname} is ${oage} minutes old (${osize} bytes) and its .sql.gz sibling is only ${ogz} bytes, under the ${MIN_BACKUP_BYTES}-byte floor. The tiny .gz is residue of the failed pg_dump attempt, not output of this dump: the pg_dump branch opens the .gz, the spawn fails, and the catch-block cleanup at backup-lib.ts:565-567 tests existsSync BEFORE the stream has created the file, so the unlink never fires and a finalized 20-byte empty frame is left behind (verification/case_214ad18c-empty-gzip-residue-repro.mjs, survives ~50-70% of attempts). The .sql beside it is the JavaScript engine's dump, which then died mid-write. This is the 2026-09-05 signature; an empty gzip frame is 20 bytes and gzip -t passes it."
    fi
  done
  [[ "$VERDICT" == "ok" ]] && VERDICT="stall"
fi

# --- Signal 3: no usable backup landed recently -----------------------------
# The signal of last resort, and the only one that does not read the log. It
# catches the case where the log itself went away: on 2026-09-05 the server
# stopped writing to server.log at 05:33, so signal 1 anchored on a stale
# completion, saw no skips after it, and called it health.
NEWEST_GOOD_NAME="none"
NEWEST_GOOD_AGE_MIN=-1
NEWEST_HOST_NAME="none"
NEWEST_HOST_AGE_MIN=-1
if (( BACKUP_DIR_READABLE == 1 || ${#BACKUP_FILES[@]} > 0 )); then
  newest_mtime=0
  newest_host_mtime=0
  selected_unknown=$(( 1 - BACKUP_DIR_READABLE ))
  excluded_archives=()
  for f in "${BACKUP_FILES[@]}"; do
    [[ "$f" == *.sql.gz ]] || continue
    select_producer "$f"
    if (( PRODUCER_SELECTED == 0 )); then
      add_code freshness producer_excluded
      excluded_archives+=("${f##*/}")
    fi
    if ! read_stat %s "$f" freshness; then
      (( PRODUCER_SELECTED == 1 )) && selected_unknown=1
      continue
    fi
    sz="$STAT_VALUE"
    (( sz >= MIN_BACKUP_BYTES )) || continue   # a measured truncated archive is not a backup
    if ! read_stat %Y "$f" freshness; then
      (( PRODUCER_SELECTED == 1 )) && selected_unknown=1
      continue
    fi
    mt="$STAT_VALUE"
    if [[ "$PRODUCER_ID" == host ]]; then
      # Host observability is independent of whether it counts for the verdict.
      if (( mt > newest_host_mtime )); then
        newest_host_mtime="$mt"
        NEWEST_HOST_NAME="${f##*/}"
        NEWEST_HOST_AGE_MIN="$STAT_AGE_MIN"
      fi
    fi
    if (( PRODUCER_SELECTED == 1 )); then
      if (( mt > newest_mtime )); then
        newest_mtime="$mt"
        NEWEST_GOOD_NAME="${f##*/}"
        NEWEST_GOOD_AGE_MIN="$STAT_AGE_MIN"
      fi
    fi
  done

  if (( ${#excluded_archives[@]} > 0 )); then
    add_note "EXCLUDED: ${#excluded_archives[@]} files from producer host; the freshness signal uses --exclude-suffix ${EXCLUDE_SUFFIX} and they cannot answer for the selected writer: ${excluded_archives[*]}"
  fi

  # A valid fresh archive proves availability despite another unknown candidate.
  # An old archive cannot prove global staleness when an unmeasured one may be
  # newer. Likewise, a failed observation is not proof of an empty directory.
  if (( BACKUP_DIR_READABLE == 1 && newest_mtime > 0 && NEWEST_GOOD_AGE_MIN <= MAX_BACKUP_AGE_HOURS * 60 )); then
    SIGNAL_STATE[freshness]=measured
  elif (( selected_unknown == 1 )); then
    add_note "UNKNOWN: selected archive observations are incomplete; absence/staleness cannot be established."
  elif (( newest_mtime == 0 )); then
    SIGNAL_STATE[freshness]=measured
    VERDICT="stall"
    add_code freshness no_usable_backup
    if (( EFFECTIVE_EXCLUDE_ENABLED == 1 )); then
      add_reason "NO USABLE BACKUP: ${BACKUP_DIR} contains no in-server ${PREFIX}-*.sql.gz (excluding *-${EXCLUDE_SUFFIX}.sql.gz) of at least ${MIN_BACKUP_BYTES} bytes. Every in-server archive present is truncated or absent."
    else
      add_reason "NO USABLE BACKUP: ${BACKUP_DIR} contains no ${PREFIX}-*.sql.gz of at least ${MIN_BACKUP_BYTES} bytes. Every archive present is truncated or absent."
    fi
  else
    SIGNAL_STATE[freshness]=measured
    [[ "$VERDICT" == "ok" ]] && VERDICT="stall"
    add_code freshness archive_stale
    add_reason "STALE: the newest usable backup ${NEWEST_GOOD_NAME} is ${NEWEST_GOOD_AGE_MIN} minutes old, over the ${MAX_BACKUP_AGE_HOURS}-hour threshold. Backups are hourly, so this is at least one missed tick regardless of what the log says."
  fi

  if (( EFFECTIVE_EXCLUDE_ENABLED == 1 && newest_host_mtime > 0 )); then
    add_note "A host-side archive (${NEWEST_HOST_NAME}, ${NEWEST_HOST_AGE_MIN} min old) exists but is excluded from this verdict by producer filter --exclude-suffix ${EXCLUDE_SUFFIX} -- it cannot answer for the in-server scheduler. See newestHostArchive / --include-all-producers."
  fi
fi

# A log whose newest bytes predate the newest good backup is a DEAD log, and an
# anchor found inside it says nothing about present health. Downgrade to
# inconclusive rather than letting a stale anchor produce a green.
# Not evaluated in --staleness-only mode: the log is never read there, so
# LOG_READABLE stays 0 by construction, not because anything failed.
LOG_STALE=0
if (( STALENESS_ONLY == 0 && LOG_READABLE == 1 && WINDOW_ANCHORED == 1 )); then
  PRODUCER_ID=in_server
  if read_stat %Y "$LOG_FILE" suppression; then
    log_age_min="$STAT_AGE_MIN"
    if (( log_age_min > MAX_BACKUP_AGE_HOURS * 60 )); then
      LOG_STALE=1
      add_code suppression log_stale
      add_note "Server log ${LOG_FILE} has not been written to for ${log_age_min} minutes, over the ${MAX_BACKUP_AGE_HOURS}-hour threshold. Its anchor is historical: the suppression signal cannot testify to present health and was NOT used to clear anything. Point --log at the live log."
    else
      SIGNAL_STATE[suppression]=measured
    fi
  fi
fi

# --- Corroborate with the server's own health view -------------------------
# Recorded for contrast, never used to clear a stall: the whole point is that
# this endpoint reads "ok" during the first 26 hours of a wedge and again
# immediately after recovery.
HEALTH_STATUS="not_checked"
HEALTH_AGE_HOURS="null"
HEALTH_LATEST="not_reported"
if (( CHECK_HEALTH == 1 )) && [[ -n "${PAPERCLIP_API_URL:-}" ]] && command -v curl >/dev/null 2>&1; then
  base="${PAPERCLIP_API_URL%/}"; base="${base%/api}"
  # BASE host validation: the bearer below is sent ONLY to this base. A base
  # that is not an http(s) URL with a non-empty bare host -- userinfo
  # (`user@host` misdirects the credential), whitespace or control
  # characters (a newline would inject a curl-config directive; any other
  # raw control corrupts the request), query/fragment, braces, a percent
  # sign in the authority (parity with the Python gate), or an authority
  # that is not one bare host[:port] or bracketed [IPv6] literal -- skips
  # this corroboration read, with a warning. Skipping is fail-closed here:
  # the stall verdict never depended on this read, and the bearer is never
  # sent to a base we refused to trust. A well-formed hostile host still
  # passes; blocking that needs an operator-owned host allowlist, not a
  # per-script guess.
  base_ok=1
  case "$base" in http://*|https://*) ;; *) base_ok=0;; esac
  if (( base_ok == 1 )); then
    case "$base" in *@*|*[[:space:]]*|*[[:cntrl:]]*|*\?*|*#*|*\{*|*\}*) base_ok=0;; esac
  fi
  if (( base_ok == 1 )); then
    _base_host="${base#http://}"; _base_host="${_base_host#https://}"; _base_host="${_base_host%%/*}"
    [[ -n "$_base_host" ]] || base_ok=0
    case "$_base_host" in :*|*,*|*%*) base_ok=0;; esac
    if (( base_ok == 1 )); then
      if [[ "$_base_host" == *\[* || "$_base_host" == *\]* ]]; then
        [[ "$_base_host" =~ ^\[[0-9a-fA-F:.]+\](:[0-9]+)?$ ]] || base_ok=0
      elif [[ "$_base_host" == *:* ]]; then
        if [[ "${_base_host%:*}" == *:* || ! "${_base_host##*:}" =~ ^[0-9]+$ ]]; then
          base_ok=0
        fi
      fi
    fi
  fi
  if (( base_ok == 1 )); then
    # Load only after argument validation and only for health corroboration.
    # shellcheck source=lib/paperclip_api_allowlist.sh
    _PC_ALLOWLIST_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/paperclip_api_allowlist.sh"
    _PC_ALLOWLIST_LIB_OK=""
    if [[ -r "$_PC_ALLOWLIST_LIB" ]]; then source "$_PC_ALLOWLIST_LIB"; _PC_ALLOWLIST_LIB_OK=1; fi
    if [[ -z "${_PC_ALLOWLIST_LIB_OK:-}" ]]; then
      echo "db_backup_stall: WARNING: API allowlist lib missing; skipping health corroboration" >&2
      base_ok=0
    else
      paperclip_allowlist_check "$base" "db_backup_stall" || base_ok=0
    fi
  fi
  if (( base_ok == 0 )); then
    echo "db_backup_stall: WARNING: PAPERCLIP_API_URL is not a clean http(s) base; skipping health corroboration" >&2
  fi
  # The UNAUTHENTICATED /api/health returns databaseBackup.status but omits
  # latestBackup entirely; only an authenticated call includes it. Send the
  # bearer when we have one so `latest`/`age` are real values rather than a
  # bare "null" that reads like "no backup exists".
  #
  # The bearer travels in a 0600 curl --config file, never on argv:
  # /proc/<pid>/cmdline is world-readable and this host is shared (case_3d790dcb).
  # Same pattern as curl_authed() in gh_token.sh -- only the config PATH is in
  # argv. The directory is per-process 0700 and removed by the EXIT trap, so a
  # file whose path only ever existed in the command-substitution subshell
  # below cannot outlive an interrupted call; the file itself is removed the
  # moment curl returns.
  health_cfg_dir=""
  cleanup_health_cfg() { [[ -n "$health_cfg_dir" ]] && rm -rf "$health_cfg_dir"; return 0; }
  health_config_args=()
  if (( base_ok == 1 )) && [[ -n "${PAPERCLIP_API_KEY:-}" ]]; then
    health_cfg_dir="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/db_backup_stall.XXXXXXXX")" \
      || { echo "ERROR: could not create a private temp directory" >&2; exit 2; }
    trap 'cleanup_health_cfg' EXIT
    trap 'cleanup_health_cfg; exit 130' INT
    trap 'cleanup_health_cfg; exit 143' TERM
    health_cfg="$(umask 077; mktemp "$health_cfg_dir/curlcfg.XXXXXXXX")" \
      || { echo "ERROR: could not create a curl config file" >&2; exit 2; }
    chmod 0600 "$health_cfg"
    printf 'header = "Authorization: Bearer %s"\n' "${PAPERCLIP_API_KEY}" > "$health_cfg"
    health_config_args=(--config "$health_cfg")
  fi
  if (( base_ok == 1 )) && health_json="$(curl -sS --max-time 20 "${health_config_args[@]}" "$base/api/health" 2>/dev/null)"; then
    if command -v python3 >/dev/null 2>&1; then
      eval "$(printf '%s' "$health_json" | python3 -c '
import json,sys
def sh(v):
    return "\x27" + str(v).replace("\x27", "\x27\\\x27\x27") + "\x27"
try:
    d = json.load(sys.stdin).get("databaseBackup") or {}
except Exception:
    print("HEALTH_STATUS=unparseable"); sys.exit(0)
print("HEALTH_STATUS=" + sh(d.get("status") or "unknown"))
lb = d.get("latestBackup") or {}
print("HEALTH_AGE_HOURS=" + sh(lb.get("ageHours") if lb.get("ageHours") is not None else "null"))
print("HEALTH_LATEST=" + sh(lb.get("name") or "null"))
' 2>/dev/null)" || HEALTH_STATUS="unparseable"
    fi
  else
    # Skipped by base validation reads as not_checked (never attempted), not
    # unreachable (attempted and failed): the two mean different things to the
    # reader of the verdict.
    if (( base_ok == 1 )); then HEALTH_STATUS="unreachable"; else HEALTH_STATUS="not_checked"; fi
  fi
  # The call is over: the bearer must not sit on disk for the rest of the run.
  # The directory itself is removed by the EXIT trap above.
  [[ -n "${health_cfg:-}" ]] && rm -f "$health_cfg"
fi

if [[ "$VERDICT" == "stall" && "$HEALTH_STATUS" == "ok" ]]; then
  add_note "/api/health reports databaseBackup.status=ok while this detector sees a stall. That is the documented blind spot, not a contradiction: the endpoint only stats .sql.gz mtimes and cannot see suppressed ticks."
fi

# --- Verdict ---------------------------------------------------------------
if [[ "$VERDICT" == "stall" ]]; then
  # Positively observed findings survive unrelated observation failures.
  EXIT_CODE=1
elif [[ "${SIGNAL_STATE[freshness]}" == unknown ]] ||
     { (( STALENESS_ONLY == 0 )) && [[ "${SIGNAL_STATE[suppression]}" == unknown || "${SIGNAL_STATE[orphan]}" == unknown ]]; }; then
  VERDICT="inconclusive"
  EXIT_CODE=2
else
  EXIT_CODE=0
fi

case "$VERDICT" in
  stall) NEXT_ACTION="Investigate the measured findings for the selected producers; this read-only verdict authorizes no restart, restore or monitor rearm." ;;
  inconclusive) NEXT_ACTION="Repair the unknown observation sources listed in codes/notes, then repeat the local-only observation; do not infer health or rearm a monitor." ;;
  *) NEXT_ACTION="No stall observed within the selected scope; keep independent restore verification and monitor acceptance separate." ;;
esac

if (( JSON == 1 )); then
  printf '{'
  printf '"schemaVersion":1,"codes":'
  json_codes "$CODES"
  printf ',"next_action":"%s","signals":{' "$(json_escape "$NEXT_ACTION")"
  comma=""
  for signal in suppression orphan freshness; do
    printf '%s"%s":{"state":"%s","codes":' "$comma" "$signal" "${SIGNAL_STATE[$signal]}"
    json_codes "${SIGNAL_CODES[$signal]}"
    printf '}'
    comma=,
  done
  printf '},"producers":{"in_server":{"selected":true},"host":{"selected":%s}},' \
    "$( (( EFFECTIVE_EXCLUDE_ENABLED == 0 )) || [[ -z "$EXCLUDE_SUFFIX" ]] && echo true || echo false )"
  printf '"verdict":"%s",' "$(json_escape "$VERDICT")"
  printf '"stalenessOnly":%s,' "$( ((STALENESS_ONLY==1)) && echo true || echo false )"
  printf '"checkedAt":"%s",' "$(json_escape "$NOW_ISO")"
  printf '"logFile":"%s",' "$(json_escape "$LOG_FILE")"
  printf '"logSizeBytes":%s,' "$LOG_SIZE_BYTES"
  printf '"windowBytes":%s,' "$WINDOW_BYTES"
  printf '"windowAnchored":%s,' "$( ((WINDOW_ANCHORED==1)) && echo true || echo false )"
  printf '"skipCountInWindow":%s,' "${SKIP_COUNT_IN_WINDOW:-0}"
  printf '"failedCountInWindow":%s,' "${FAILED_COUNT_IN_WINDOW:-0}"
  printf '"lastCompleteLine":"%s",' "$(json_escape "$LAST_COMPLETE_LINE")"
  printf '"lastSkipLine":"%s",' "$(json_escape "$LAST_SKIP_LINE")"
  printf '"orphanCount":%s,' "${#ORPHANS[@]}"
  printf '"logStale":%s,' "$( ((LOG_STALE==1)) && echo true || echo false )"
  printf '"newestUsableBackup":"%s",' "$(json_escape "$NEWEST_GOOD_NAME")"
  printf '"newestUsableBackupAgeMinutes":%s,' "$NEWEST_GOOD_AGE_MIN"
  printf '"excludeSuffix":"%s",' "$(json_escape "$EXCLUDE_SUFFIX")"
  printf '"excludeEnabled":%s,' "$( ((EFFECTIVE_EXCLUDE_ENABLED==1)) && echo true || echo false )"
  printf '"newestHostArchive":"%s",' "$(json_escape "$NEWEST_HOST_NAME")"
  printf '"newestHostArchiveAgeMinutes":%s,' "$NEWEST_HOST_AGE_MIN"
  printf '"minBackupBytes":%s,' "$MIN_BACKUP_BYTES"
  printf '"maxBackupAgeHours":%s,' "$MAX_BACKUP_AGE_HOURS"
  printf '"healthStatus":"%s",' "$(json_escape "$HEALTH_STATUS")"
  printf '"healthLatestBackup":"%s",' "$(json_escape "$HEALTH_LATEST")"
  printf '"healthAgeHours":"%s",' "$(json_escape "$HEALTH_AGE_HOURS")"
  printf '"reasons":['
  for i in "${!REASONS[@]}"; do
    (( i > 0 )) && printf ','
    printf '"%s"' "$(json_escape "${REASONS[$i]}")"
  done
  printf '],"notes":['
  for i in "${!NOTES[@]}"; do
    (( i > 0 )) && printf ','
    printf '"%s"' "$(json_escape "${NOTES[$i]}")"
  done
  printf '],"exitCode":%s}\n' "$EXIT_CODE"
else
  case "$VERDICT" in
    stall)        printf 'DB BACKUP STALL DETECTED\n' ;;
    inconclusive) printf 'INCONCLUSIVE -- this run cannot confirm backup health\n' ;;
    *)            printf 'No backup stall detected\n' ;;
  esac
  printf '  checked at         %s\n' "$NOW_ISO"
  if (( STALENESS_ONLY == 1 )); then
    printf '  mode               staleness-only (log and orphan signals not evaluated)\n'
  else
    printf '  log                %s (%s bytes, window %s)\n' "$LOG_FILE" "$LOG_SIZE_BYTES" "$WINDOW_BYTES"
    printf '  window anchored    %s\n' "$( ((WINDOW_ANCHORED==1)) && echo yes || echo 'NO -- cannot conclude health' )"
    printf '  log freshness      %s\n' "$( if (( LOG_STALE == 1 )); then echo 'STALE -- anchor is historical'; elif [[ "${SIGNAL_STATE[suppression]}" == measured ]]; then echo current; else echo 'UNKNOWN -- cannot conclude health'; fi )"
    printf '  skips in window    %s\n' "${SKIP_COUNT_IN_WINDOW:-0}"
    printf '  orphaned/truncated %s\n' "${#ORPHANS[@]}"
  fi
  printf '  newest usable .gz  %s (%s min old, floor %s bytes)\n' \
    "$NEWEST_GOOD_NAME" "$NEWEST_GOOD_AGE_MIN" "$MIN_BACKUP_BYTES"
  if [[ "$NEWEST_HOST_NAME" != "none" ]]; then
    printf '  newest host .gz    %s (%s min old, suffix -%s, %s)\n' \
      "$NEWEST_HOST_NAME" "$NEWEST_HOST_AGE_MIN" "$EXCLUDE_SUFFIX" \
      "$( ((EFFECTIVE_EXCLUDE_ENABLED==1)) && echo 'excluded from verdict' || echo 'counted toward verdict' )"
  fi
  printf '  /api/health says   %s (latest %s, age %sh)\n' "$HEALTH_STATUS" "$HEALTH_LATEST" "$HEALTH_AGE_HOURS"
  if [[ -n "$LAST_COMPLETE_LINE" ]]; then
    printf '  last completion    %s\n' "${LAST_COMPLETE_LINE:0:150}"
  fi
  if (( ${#REASONS[@]} > 0 )); then
    printf '\nFindings:\n'
    for r in "${REASONS[@]}"; do printf '  - %s\n' "$r"; done
  fi
  if (( ${#NOTES[@]} > 0 )); then
    printf '\nNotes:\n'
    for n in "${NOTES[@]}"; do printf '  - %s\n' "$n"; done
  fi
  if [[ "$VERDICT" == "stall" && "$STALENESS_ONLY" == 0 ]]; then
    printf '\nRemediation: the in-flight guard only clears on process restart until the\n'
    printf 'watchdog in patches/case_14da0d3f-db-backup-watchdog.patch is deployed. Restarting\n'
    printf 'the Paperclip server clears databaseBackupInFlight and the next tick backs up.\n'
  elif [[ "$VERDICT" == "stall" ]]; then
    printf '\nRemediation: no usable archive has landed within --max-backup-age-hours.\n'
    printf 'Check both writers: the in-server scheduler (docs/runbooks/database-backup-stall.md)\n'
    printf 'and the host-side fallback (docs/host-db-backup.md), e.g.\n'
    printf '  systemctl --user status paperclip-host-db-backup.timer\n'
  fi
fi

exit "$EXIT_CODE"
