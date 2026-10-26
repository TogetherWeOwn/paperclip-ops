#!/usr/bin/env bash
set -uo pipefail

# ===========================================================================
# Drain the logs of FAILED jobs into one compact bundle  (case_821f1653)
#
# THE PROBLEM THIS EXISTS FOR. When a CI run goes red, triage means opening
# every failed job, paging through thousands of log lines, and finding the one
# that names the cause. Reviewers do not do that in minutes; they re-run, or
# theyskim the checks list and guess. This tool drains each failed job's log
# into a single artifact directory, trims every log to its tail, and prints a
# one-line summary per failing job that names the job, the step that failed,
# and the first error-shaped line — so a red run triages from the summary.
#
# With --json, the same drain also writes failure-summary.json: a versioned
# envelope (schema_version, run id, per-job rows, summary counts, error
# triple) so triage parses instead of scraping the md. The md bytes, the
# ::error line and stdout are identical with the flag on or off.
#
# WHERE IT RUNS. As the `failure-log-drain` job in ci.yml, gated on
# `if: failure()` with `needs:` on every suite job, so it runs exactly once
# per red run, after all suites have finished, and never on green runs or on
# superseded (cancelled) runs. It uploads its output dir with
# actions/upload-artifact. It never fails the build for its own findings —
# the run is already red; this only describes it. It REFUSES (exit 3) when it
# cannot observe the job list, because a drain that measured nothing must
# never print "no failures found" (case_70ae2474).
#
# Reads: GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT, GH_TOKEN
#        (the API reads need actions:read; the drain job's token carries
#        actions:write because upload-artifact needs it to finalise).
# Requires curl and jq. The token travels in a header FILE, never in argv —
# /proc/*/cmdline is world-readable and this host is shared (case_3d790dcb).
# REDIRECT SCOPING: the log endpoint answers 302 to pre-signed storage and
# curl -L follows it — but since curl 7.58.0 (CVE-2018-1000007), plain -L does
# NOT re-send a custom Authorization header (header file included) to a
# different host unless --location-trusted is given. This script passes no
# --location-trusted, assumes curl >= 7.58.0, and runs on ubuntu-latest
# (curl 8.x), so the storage target never receives this bearer; pre-7.58 curl
# is the only version that forwards. The redirect residual is what DOES
# travel: the server-chosen target URL itself, non-credential headers, and a
# possible protocol downgrade. Any future host allowlist here must therefore
# cover redirect targets with manual redirect handling and an explicit
# target-host check (the protection-rule/src/github.js pattern: redirect
# manual, token to the API origin only) — never with --proto-redir, which
# restricts redirect PROTOCOLS only. Do not copy this -L+auth pattern to a
# wider-scoped bearer without that handling.
# Every curl call below carries --connect-timeout/--max-time (case_e107fefc): a
# hung endpoint must refuse (exit 3, HTTP 000 reads as a failed measurement),
# never print "no failures found" over an unmeasured job list.
# ===========================================================================

ME="$(basename "${BASH_SOURCE[0]}")"

# Compactness caps. A drain that ships whole 20k-line logs is not a drain.
LOG_TAIL_LINES=400
SUMMARY_LINE_CHARS=300

usage() {
  cat <<'USAGE'
  ./ci_failure_drain.sh drain [--jobs-json FILE --logs-dir DIR] [--out-dir DIR] [--json]
  ./ci_failure_drain.sh help

Drains the logs of failed jobs in the current run into OUT_DIR:
  failure-summary.md   one line per failing job (job, step, cause)
  <job-name>.log       last LOG_TAIL_LINES lines of that job's log
  failure-summary.json with --json only: versioned envelope (schema_version,
                       run id, per-job rows, summary counts, error triple)

Without flags the job list and logs come from the Actions API (CI use).
With --jobs-json/--logs-dir they come from files (hermetic tests).
With --json the md bytes, the ::error line and stdout are unchanged.
If optional JSON publication fails, stderr warns and any prior JSON is kept;
the drain exit code is unchanged.

EXIT CODES
  0  drained (whether or not failures were found)
  2  usage error
  3  REFUSED — the job list or a failed job's log could not be observed.
USAGE
}

die()    { printf '%s: %s\n' "$ME" "$1" >&2; exit "${2:-2}"; }
# refuse MSG CODE UNMEASURED: with --json, record the refusal — which input
# went unmeasured — in failure-summary.json before exiting 3, so an
# unmeasured input leaves a parseable trace and never reads as clean.
refuse() {
  if [[ "${JSON_OUT:-0}" == "1" ]]; then
    write_refusal_json "${2:-refused_unobservable}" "${3:-unknown}"
  fi
  printf '%s: REFUSED: %s\n' "$ME" "$1" >&2; exit 3
}

# --- failure-summary.json envelope (opt-in via --json) -------------------------
# Built with jq so causes carrying quotes or backslashes stay valid JSON.
# Stage beside the destination so a successful rename publishes atomically.
# -T refuses a directory target instead of moving the staging file inside it.
# JSON is optional: warn on failure, preserving prior evidence and drain exits.
write_json() {
  local staged
  if [[ "$JSON_ROWS_FAILED" == "0" ]] && staged="$(mktemp "$OUT_DIR/.failure-summary.json.XXXXXX")"; then
    if "$@" > "$staged" && mv -fT -- "$staged" "$OUT_DIR/failure-summary.json"; then
      return 0
    fi
    rm -f -- "$staged"
  fi
  printf '%s: warning: unable to publish optional failure-summary.json\n' "$ME" >&2
  return 1
}

# write_envelope STATUS NEXT_ACTION DRAINED_COUNT: the success envelope over
# the rows the drain loop collected ($TMP/rows.jsonl, empty on green runs).
write_envelope() {
  local total
  total="$(jq '.jobs | length' "$TMP/jobs.json")"
  [[ -f "$TMP/rows.jsonl" ]] || : > "$TMP/rows.jsonl"
  # A quoted heredoc keeps jq variables literal through the writer wrapper.
  write_json jq -n --argjson schema_version 1 --arg run_id "$RUN_ID" --arg status "$1" \
    --slurpfile jobs "$TMP/rows.jsonl" \
    --argjson total "$total" --argjson failed "$N_FAILED" --argjson drained "$3" \
    --arg next "$2" -f /dev/stdin <<'JQ'
{schema_version:$schema_version,run_id:$run_id,status:$status,jobs:$jobs,summary:{total_jobs:$total,failed_jobs:$failed,drained_jobs:$drained},error:{code:"ok",retryable:false,next_action:$next,unmeasured:null}}
JQ
}

# write_refusal_json CODE UNMEASURED: the refusal envelope. Counts reflect
# what was actually measured: zero when the job list itself is unobservable,
# the measured list totals when a failed job's log is the missing input.
write_refusal_json() {
  local total=0 failed=0 next="provide_jobs_list" run="${GITHUB_RUN_ID:-fixture}"
  if [[ -s "$TMP/jobs.json" ]] && jq -e '.jobs | type == "array"' "$TMP/jobs.json" >/dev/null 2>&1; then
    total="$(jq '.jobs | length' "$TMP/jobs.json")"
    failed="$(jq '[.jobs[] | select(.conclusion=="failure")] | length' "$TMP/jobs.json")"
  fi
  [[ "$1" == "refused_log_missing" ]] && next="fetch_missing_log"
  # A quoted heredoc keeps jq variables literal through the writer wrapper.
  write_json jq -n --argjson schema_version 1 --arg run_id "$run" \
    --arg code "$1" --arg unmeasured "$2" --arg next "$next" \
    --argjson total "$total" --argjson failed "$failed" -f /dev/stdin <<'JQ'
{schema_version:$schema_version,run_id:$run_id,status:"refused",jobs:[],summary:{total_jobs:$total,failed_jobs:$failed,drained_jobs:0},error:{code:$code,retryable:false,next_action:$next,unmeasured:$unmeasured}}
JQ
}

[[ $# -ge 1 ]] || { usage >&2; exit 2; }
CMD="$1"; shift
case "$CMD" in
  help|--help|-h) usage; exit 0 ;;
  drain) ;;
  *) usage >&2; exit 2 ;;
esac

JOBS_JSON=""
LOGS_DIR=""
OUT_DIR="failure-drain"
JSON_OUT=0
JSON_ROWS_FAILED=0
API="${GITHUB_API_URL:-https://api.github.com}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --jobs-json) [[ $# -ge 2 ]] || die "--jobs-json needs a path"; JOBS_JSON="$2"; shift 2 ;;
    --logs-dir)  [[ $# -ge 2 ]] || die "--logs-dir needs a path";  LOGS_DIR="$2";  shift 2 ;;
    --out-dir)   [[ $# -ge 2 ]] || die "--out-dir needs a path";   OUT_DIR="$2";   shift 2 ;;
    --json) JSON_OUT=1; shift ;;
    *) die "unexpected argument: $1" ;;
  esac
done
# The two fixture flags travel together: half a fixture is a lie.
if [[ -n "$JOBS_JSON$LOGS_DIR" && ( -z "$JOBS_JSON" || -z "$LOGS_DIR" ) ]]; then
  die "--jobs-json and --logs-dir must be given together"
fi

for c in jq curl; do
  command -v "$c" >/dev/null 2>&1 || die "$c is required"
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$OUT_DIR"

# --- obtain the job list ------------------------------------------------------
if [[ -n "$JOBS_JSON" ]]; then
  [[ -s "$JOBS_JSON" ]] || refuse "jobs json is missing or empty: $JOBS_JSON" "refused_jobs_unobservable" "jobs"
  cp "$JOBS_JSON" "$TMP/jobs.json"
else
  : "${GITHUB_REPOSITORY:?}" 2>/dev/null || refuse "GITHUB_REPOSITORY is unset; not running in Actions" "refused_jobs_unobservable" "jobs"
  [[ -n "${GITHUB_RUN_ID:-}" ]] || refuse "GITHUB_RUN_ID is unset" "refused_jobs_unobservable" "jobs"
  [[ -n "${GH_TOKEN:-}" ]]      || refuse "GH_TOKEN is unset; actions:read is required to list jobs" "refused_jobs_unobservable" "jobs"

  ATTEMPT="${GITHUB_RUN_ATTEMPT:-1}"
  HDR="$TMP/hdr"; umask 077
  printf 'Authorization: Bearer %s\n' "$GH_TOKEN" > "$HDR"
  printf 'Accept: application/vnd.github+json\n' >> "$HDR"

  URL="$API/repos/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID/attempts/$ATTEMPT/jobs?per_page=100"
  # A timeout AFTER the '200' header still prints CODE 200 via -w while curl
  # exits 28 with a truncated body (case_32a28c0d): refuse on the transport
  # status first, never on the code alone.
  CURL_RC=0
  CODE="$(curl -sS --connect-timeout 10 --max-time 30 -o "$TMP/jobs.json" -w '%{http_code}' -H @"$HDR" "$URL" 2>"$TMP/curl.err")" || CURL_RC=$?
  [[ "$CURL_RC" -eq 0 ]] || refuse "listing jobs: curl transport failure (exit $CURL_RC)" "refused_jobs_unobservable" "jobs"
  [[ "$CODE" == "200" ]] || refuse "listing jobs returned HTTP $CODE (actions:read missing?)" "refused_jobs_unobservable" "jobs"
fi

jq -e '.jobs | type == "array" and length > 0' "$TMP/jobs.json" >/dev/null 2>&1 \
  || refuse "the jobs response carries no job list; nothing was measured" "refused_jobs_unobservable" "jobs"

# --- classify -----------------------------------------------------------------
RUN_ID="${GITHUB_RUN_ID:-fixture}"
FAILED_IDS="$(jq -r '[.jobs[] | select(.conclusion=="failure") | .id] | .[]' "$TMP/jobs.json")"
N_FAILED="$(jq '[.jobs[] | select(.conclusion=="failure")] | length' "$TMP/jobs.json")"

emit() { printf '%s\n' "$1"; [[ -n "${GITHUB_STEP_SUMMARY:-}" ]] && printf '%s\n' "$1" >> "$GITHUB_STEP_SUMMARY"; return 0; }

# One line per failing job: name the job, the step that failed, and the cause.
# Error-shaped means it names something that broke, not just the last line of
# a log (which is usually teardown noise).
cause_of() {
  local log="$1"
  local line
  line="$(grep -a -m1 -i -E '::error|failed|error:|traceback|assertion' "$log" 2>/dev/null || true)"
  [[ -n "$line" ]] || line="$(grep -a . "$log" 2>/dev/null | tail -n 1 || true)"
  # Strip the Actions timestamp prefix; it doubles every line's width.
  line="$(sed -E 's/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z //' <<< "$line")"
  # Slice characters, not bytes, even when the caller runs in the C locale.
  local LC_ALL=C.UTF-8
  printf '%s' "${line:0:$SUMMARY_LINE_CHARS}"
}

safe_name() {
  # The hyphen sits last so tr reads it as a literal, not a range
  # endpoint — a reversed range would silently mangle every job name.
  # printf, not a herestring: `<<<` appends a newline that tr would turn
  # into a trailing underscore on every filename.
  printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_' | cut -c1-80
}

{
  printf '# CI failure drain — run %s\n\n' "$RUN_ID"
} > "$OUT_DIR/failure-summary.md"

if [[ "$N_FAILED" -eq 0 ]]; then
  printf 'No failed jobs in this run.\n' >> "$OUT_DIR/failure-summary.md"
  emit "### ✅ Failure drain: no failed jobs in run ${RUN_ID}"
  [[ "$JSON_OUT" == "1" ]] && write_envelope "no_failures" "none" 0
  exit 0
fi

SUMMARY_NAMES=()
for jid in $FAILED_IDS; do
  JNAME="$(jq -r --argjson id "$jid" '.jobs[] | select(.id==$id) | .name // "job"' "$TMP/jobs.json")"
  STEP="$(jq -r --argjson id "$jid" '[.jobs[] | select(.id==$id) | .steps[]? | select(.conclusion=="failure")] | first | .name // ""' "$TMP/jobs.json")"
  FNAME="$(safe_name "$JNAME").log"

  if [[ -n "$LOGS_DIR" ]]; then
    [[ -s "$LOGS_DIR/$jid.log" ]] || refuse "no fixture log for failed job $jid ($JNAME)" "refused_log_missing" "$jid"
    SRC="$LOGS_DIR/$jid.log"
  else
    umask 077
    HDR="$TMP/hdr"; [[ -f "$HDR" ]] || { printf 'Authorization: Bearer %s\n' "$GH_TOKEN" > "$HDR"; }
    LOGURL="$API/repos/$GITHUB_REPOSITORY/actions/jobs/$jid/logs"
    # Same case_32a28c0d contract as the jobs list: a timed-out '200' carries a
    # truncated log, so the transport status refuses before the code is read.
    CURL_RC=0
    CODE="$(curl -sS -L --connect-timeout 10 --max-time 60 -o "$TMP/$jid.log" -w '%{http_code}' -H @"$HDR" -H 'Accept: application/vnd.github+json' "$LOGURL" 2>"$TMP/curl-$jid.err")" || CURL_RC=$?
    [[ "$CURL_RC" -eq 0 ]] || refuse "downloading the log for failed job $jid ($JNAME): curl transport failure (exit $CURL_RC)" "refused_log_missing" "$jid"
    # The log endpoint answers 302 to pre-signed storage; -L follows it to 200.
    # On curl >= 7.58 Authorization stays with the API host (see REDIRECT
    # SCOPING); the redirect residual is the target URL itself, not this bearer.
    [[ "$CODE" == "200" ]] || refuse "downloading the log for failed job $jid ($JNAME) returned HTTP $CODE" "refused_log_missing" "$jid"
    SRC="$TMP/$jid.log"
  fi

  tail -n "$LOG_TAIL_LINES" "$SRC" > "$OUT_DIR/$FNAME"
  CAUSE="$(cause_of "$OUT_DIR/$FNAME")"
  # The envelope row reuses the md's own cause/step/filename, so the two
  # surfaces cannot triage differently.
  if [[ "$JSON_OUT" == "1" ]]; then
    if ! jq -n --argjson id "$jid" --arg name "$JNAME" --arg step "$STEP" \
      --arg cause "$CAUSE" --arg log "$FNAME" \
      '{id:$id,name:$name,failed_step:$step,cause:$cause,log_file:$log}' >> "$TMP/rows.jsonl"; then
      # Even valid output from a failed serializer must veto either envelope.
      JSON_ROWS_FAILED=1
    fi
  fi
  [[ -n "$STEP" ]] && STEP_TXT=" at step \"$STEP\"" || STEP_TXT=""
  printf -- '- ❌ %s%s: %s\n  Log: %s (last %s lines)\n' "$JNAME" "$STEP_TXT" "$CAUSE" "$FNAME" "$LOG_TAIL_LINES" >> "$OUT_DIR/failure-summary.md"
  LABEL="$JNAME$STEP_TXT"
  SUMMARY_NAMES+=("$LABEL")
  printf 'drain: %s — %s\n' "$LABEL" "$CAUSE"
done

[[ "$JSON_OUT" == "1" ]] && write_envelope "drained" "triage_failures" "$N_FAILED"

# The annotation is the triage surface: it names the failing jobs on the run
# summary itself, so nobody opens the artifact to learn which job broke.
ANNOT="$(printf '%s; ' "${SUMMARY_NAMES[@]}")"
ANNOT="${ANNOT//'%'/'%25'}"
ANNOT="${ANNOT//$'\r'/'%0D'}"
ANNOT="${ANNOT//$'\n'/'%0A'}"
printf '::error title=Failure drain: %s job(s) failed::%s See the failure-drain artifact for trimmed logs.\n' \
  "$N_FAILED" "$ANNOT"

emit "### ❌ Failure drain: ${N_FAILED} job(s) failed in run ${RUN_ID}"
emit ""
while IFS= read -r line; do emit "$line"; done < "$OUT_DIR/failure-summary.md"

exit 0
