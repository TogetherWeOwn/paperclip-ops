#!/usr/bin/env bash
# push-hindsight-recall.sh — cron: Hindsight recall canary → Gatus.
#
# case_34ee2cf0 (parent case_66436e78 item 5, dependencies).
# Source-only: no host deploy rides this change; the operator installs the
# cron line separately (see OPERATOR.md).
#
# What it does, once per run (cron: */5 * * * *):
#   1. POSTs the constant canary query in HINDSIGHT_CANARY_QUERY to the
#      recall URL in HINDSIGHT_RECALL_URL (full URL, no path invented here)
#      with the bearer in HINDSIGHT_RECALL_BEARER, bounded by
#      HINDSIGHT_RECALL_TIMEOUT_S (default 60: deliberately above the
#      45s latency threshold so a slow-but-full reply is observable as
#      latency breach, not curl-killed as probe failure).
#   2. Asserts HTTP 200 AND a non-empty result set in the JSON reply.
#   3. Pushes two Gatus external endpoints through the shared
#      ../case_658bf6b9/_gatus_push.sh helper:
#        hindsight_recall-canary  — success iff 200 + non-empty result
#        hindsight_recall-latency — success iff round-trip ms <= threshold
#      On breach the latency message carries the measured ms, so the
#      status page shows latency alongside up/down.
#
# FAIL-CLOSED: any non-200, curl failure, or timeout pushes success=false
# for BOTH endpoints and exits 1. A 200 with an empty or unparseable
# result pushes canary DOWN while latency stays truthful — a fast-but-empty
# reply reads as content failure, not speed failure — and still exits 1,
# so a blind or empty monitor never reads green. Missing env or missing
# helper refuses (exit 2) before any health claim.
#
# READ-ONLY / NO SECRETS: the probe only recalls; it never retains. Push
# messages carry the HTTP status, the result count and the latency — never
# recalled memory text, never the query, never the bearer. Bearers travel
# in 0600 curl config files, never argv; the canary query travels via
# environ to the JSON writer, never argv either (/proc/<pid>/cmdline is
# world-readable, environ is same-uid only). This script never prints a
# bearer.
#
# Env:
#   HINDSIGHT_RECALL_URL      full recall URL (required)
#   HINDSIGHT_RECALL_BEARER   recall bearer (required)
#   HINDSIGHT_CANARY_QUERY    fixed canary query (required; constant, so a
#                             canary that stops matching is itself signal)
#   HINDSIGHT_RECALL_TIMEOUT_S probe timeout, seconds (default 60; must
#                             exceed HINDSIGHT_LATENCY_MS so the
#                             latency-only verdict stays reachable)
#   HINDSIGHT_LATENCY_MS      latency threshold, ms (default 45000)
#   GATUS_URL, GATUS_EXTERNAL_TOKEN (via _gatus_push.sh)
#   GATUS_PUSH_TIMEOUT        Gatus push timeout, seconds (default 10)
#
# Exit: 0 both endpoints healthy and pushed; 1 DOWN pushed (breach or probe
# failure); 2 refusal (bad env, missing helper/tooling) before any claim.
#   push-hindsight-recall.sh [-h|--help]
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../case_658bf6b9/_gatus_push.sh
GATUS_HELPER="$HERE/../case_658bf6b9/_gatus_push.sh"
[[ -f "$GATUS_HELPER" ]] \
  || { echo "push-hindsight-recall: refusing — shared helper $GATUS_HELPER missing" >&2; exit 2; }
# shellcheck disable=SC1090
. "$GATUS_HELPER"

for arg in "$@"; do
  case "$arg" in
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "push-hindsight-recall: refusing — unknown argument '$arg'" >&2; exit 2 ;;
  esac
done

command -v curl >/dev/null 2>&1 \
  || { echo "push-hindsight-recall: refusing — curl not found" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 \
  || { echo "push-hindsight-recall: refusing — python3 required" >&2; exit 2; }

if [[ -z "${HINDSIGHT_RECALL_URL:-}" ]]; then
  echo "push-hindsight-recall: refusing — HINDSIGHT_RECALL_URL is unset" >&2; exit 2
fi
if [[ -z "${HINDSIGHT_RECALL_BEARER:-}" ]]; then
  echo "push-hindsight-recall: refusing — HINDSIGHT_RECALL_BEARER is unset" >&2; exit 2
fi
if [[ -z "${HINDSIGHT_CANARY_QUERY:-}" ]]; then
  echo "push-hindsight-recall: refusing — HINDSIGHT_CANARY_QUERY is unset" >&2; exit 2
fi
[[ -n "${GATUS_URL:-}" ]] \
  || { echo "push-hindsight-recall: refusing — GATUS_URL is unset" >&2; exit 2; }
[[ -n "${GATUS_EXTERNAL_TOKEN:-}" ]] \
  || { echo "push-hindsight-recall: refusing — GATUS_EXTERNAL_TOKEN is unset" >&2; exit 2; }

TIMEOUT_S="${HINDSIGHT_RECALL_TIMEOUT_S:-60}"
[[ "$TIMEOUT_S" =~ ^[0-9]+$ ]] \
  || { echo "push-hindsight-recall: refusing — HINDSIGHT_RECALL_TIMEOUT_S must be whole seconds" >&2; exit 2; }
LATENCY_MS="${HINDSIGHT_LATENCY_MS:-45000}"
[[ "$LATENCY_MS" =~ ^[0-9]+$ ]] \
  || { echo "push-hindsight-recall: refusing — HINDSIGHT_LATENCY_MS must be whole ms" >&2; exit 2; }
# The timeout must exceed the latency threshold, otherwise curl kills
# every reply that would breach it and the latency-only (slow-but-full,
# capacity-pressure) verdict can never fire — a saturated recall would
# page as Hindsight-down instead of capacity pressure.
if (( TIMEOUT_S * 1000 <= LATENCY_MS )); then
  echo "push-hindsight-recall: refusing — HINDSIGHT_RECALL_TIMEOUT_S (${TIMEOUT_S}s) must exceed HINDSIGHT_LATENCY_MS (${LATENCY_MS}ms) so slow-but-full replies stay observable" >&2; exit 2
fi

# --- probe ---------------------------------------------------------------
# The query rides in a 0600 JSON file, the bearer in a 0600 curl config —
# neither ever touches argv. The reply lands in a 0600 file too: it may
# hold recalled memory text, which must never reach the status page or
# the cron log.
probe_dir="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/hindsight_canary.XXXXXXXX")" \
  || { echo "push-hindsight-recall: refusing — no private temp dir" >&2; exit 2; }
# Top level: RETURN fires only in functions, so EXIT removes the dir
# holding both credential-bearing configs on every path.
# shellcheck disable=SC2064
trap "rm -rf '$probe_dir'" EXIT
query_file="$probe_dir/query.json"
body_file="$probe_dir/body.txt"
probe_cfg="$probe_dir/curlcfg.probe"
# The query travels via environ, never argv: /proc/<pid>/cmdline is
# world-readable, while environ is same-uid only. Only the query *file*
# path rides argv — paths are not secrets.
python3 -c 'import json,os,sys; json.dump({"query": os.environ["HINDSIGHT_CANARY_QUERY"]}, open(sys.argv[1], "w"))' \
  "$query_file" \
  || { echo "push-hindsight-recall: refusing — cannot write query file" >&2; exit 2; }
# Pre-create the reply file 0600: it will hold recalled memory text.
: > "$body_file"
chmod 0600 "$query_file" "$body_file"
{
  printf 'url = "%s"\n' "$HINDSIGHT_RECALL_URL"
  printf 'request = "POST"\n'
  printf 'header = "Content-Type: application/json"\n'
  printf 'header = "Authorization: Bearer %s"\n' "$HINDSIGHT_RECALL_BEARER"
  printf 'data-binary = "@%s"\n' "$query_file"
  printf 'output = "%s"\n' "$body_file"
  printf 'write-out = "%%{http_code} %%{time_total}"\n'
  printf 'max-time = "%s"\n' "$TIMEOUT_S"
  printf 'silent\nshow-error\n'
} > "$probe_cfg"
chmod 0600 "$probe_cfg"

start_ms="$(date +%s%3N 2>/dev/null || echo "")"
probe_out="$(curl --config "$probe_cfg" 2>&1)" \
  || {
    gatus_push hindsight_recall-canary false "probe failed: curl exit ($probe_out)" || true
    gatus_push hindsight_recall-latency false "probe failed: curl exit (fail-closed DOWN)" || true
    exit 1
  }
http="${probe_out%% *}"
if [[ -z "$start_ms" ]]; then
  # %s%3N unsupported (busybox date): fall back to whole seconds.
  elapsed_ms=""
else
  end_ms="$(date +%s%3N)"
  elapsed_ms=$(( end_ms - start_ms ))
fi
if [[ -z "${elapsed_ms:-}" ]]; then
  elapsed_ms="unknown"
fi

# --- evaluate -------------------------------------------------------------
# Counts results without echoing recalled text: the python pass prints one
# verdict line only — "<ok|empty|malformed> <count-or-reason>".
verdict="$(python3 - "$body_file" <<'PY' 2>&1
import json, sys
try:
    doc = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as exc:
    print(f"malformed unparseable reply ({exc})")
    sys.exit(0)
if isinstance(doc, dict):
    for key in ("results", "memories", "items", "hits"):
        val = doc.get(key)
        if isinstance(val, list):
            print(f"{'ok' if val else 'empty'} {len(val)} {key}")
            sys.exit(0)
    if isinstance(doc.get("data"), list):
        val = doc["data"]
        print(f"{'ok' if val else 'empty'} {len(val)} data")
        sys.exit(0)
    print("malformed no result array in reply")
elif isinstance(doc, list):
    print(f"{'ok' if doc else 'empty'} {len(doc)} top-level array")
else:
    print("malformed reply is neither object nor array")
PY
)"
verdict_kind="${verdict%% *}"
verdict_rest="${verdict#* }"

# Latency verdict, shared by every path: messages carry ms only — never
# recalled text, never the query. Unknown elapsed (no %3N clock) pushes
# true: latencyUNKNOWN is not latencyBREACH; the canary endpoint owns
# content failures and the heartbeat owns silent ones.
push_latency() {  # <context> — e.g. "2 results" or "empty 0 results"
  local context="$1"
  if [[ "$elapsed_ms" =~ ^[0-9]+$ ]] && (( elapsed_ms > LATENCY_MS )); then
    gatus_push hindsight_recall-latency false "recall ${elapsed_ms}ms over ${LATENCY_MS}ms ($context)"
  elif [[ "$elapsed_ms" =~ ^[0-9]+$ ]]; then
    gatus_push hindsight_recall-latency true
  else
    gatus_push hindsight_recall-latency true
  fi
}

rc=0
if [[ "$http" != "200" ]]; then
  gatus_push hindsight_recall-canary false "recall HTTP $http (fail-closed DOWN)" || rc=1
  gatus_push hindsight_recall-latency false "recall HTTP $http (fail-closed DOWN)" || rc=1
  exit 1
fi
if [[ "$verdict_kind" != "ok" ]]; then
  # 200 but empty or malformed: canary names no rows (empty) or the reply
  # shape drifted (malformed). The canary endpoint goes DOWN; latency
  # stays truthful so a slow-but-empty reply reads as content failure,
  # not speed failure.
  gatus_push hindsight_recall-canary false "recall 200 but $verdict (fail-closed DOWN)" || rc=1
  push_latency "$verdict" || rc=1
  exit 1
fi
# Healthy content: canary verdict keys the recall endpoint; latency keys
# the latency endpoint. The message holds count + ms only — never text.
gatus_push hindsight_recall-canary true || rc=1
push_latency "$verdict_rest" || rc=1
[[ "$elapsed_ms" =~ ^[0-9]+$ ]] && (( elapsed_ms > LATENCY_MS )) && rc=1
exit "$rc"
