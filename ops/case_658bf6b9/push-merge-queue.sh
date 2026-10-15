#!/usr/bin/env bash
# push-merge-queue.sh — cron: merge-queue depth + oldest-enqueue wait →
# Gatus (`platform_merge-queue`).
#
# Per repo in GH_REPOS (space-separated owner/repo list), GETs
# repos/<repo>/merge-queue/<MQ_BRANCH> (default main) and reads the
# `entries` array: queue DEPTH (entry count) plus oldest-enqueue WAIT (now
# minus each entry's `enqueuedAt`). Entries without a parseable `enqueuedAt`
# count toward depth but not toward wait — wait reads "unknown" (never
# breaching alone), depth still enforces. The response root must be an
# object with an `entries` array: a bare array or any other shape is
# corrupt and fails closed.
#
# ALERT PROPOSAL WITH HYSTERESIS: alert on depth (MQ_DEPTH_ALERT, default
# 5) or wait (MQ_WAIT_ALERT_MIN, default 120); clear at/below
# MQ_DEPTH_CLEAR (default 2) and MQ_WAIT_CLEAR_MIN (default 60). The alert
# band trips on EITHER signal; the clear band needs BOTH quiet; between
# the bands the previous verdict LATCHES (MQ_STATE_FILE, default
# $HOME/.cache/merge-queue-state.json) so a draining queue cannot flap.
# Unreadable state degrades to stateless (alert still pages), never silent.
# Clear-band defaults must sit strictly below the alert band; equal or
# crossed bands refuse (exit 2). Why 120m: merge-group checks run the full
# CI matrix (~70m Long mutation gates); 120m is ~2x the longest suite, so a
# legitimately slow entry never pages while a case_a97273bd-scale stall (190m)
# always does. No auto-merge, no runner change — report only.
#
# QUEUE ABSENT: a repo whose merge-queue endpoint 404s has no queue (today:
# every repo — the case_082e9f7b trigger is inert until the merge-queue rule
# exists). MQ_REQUIRE_QUEUE=0 (default) reads that HEALTHY with a
# "not enabled" note; =1 reads DEGRADED. Other transport failures fail
# closed (exit 1).
#
# JSON VERDICT: prints one line of case_401f7b0d-merge-queue-v1 JSON to stdout
# (the cron log), so the module returns JSON on fixtures:
# {"schema":"case_401f7b0d-merge-queue-v1","verdict":{...}}.
#
# FAIL-CLOSED: a gh failure, an unparseable response, or a queue deeper
# than MQ_PAGE_LIMIT pages (default 10, per_page 100) pushes
# success=false; a DOWN endpoint exits 1 so cron reports it. Missing
# GH_REPOS / GATUS_URL / GATUS_EXTERNAL_TOKEN, a bad branch name, or
# crossed hysteresis bands refuses (exit 2) before touching the network.
# MQ_KEY_SUFFIX must be empty or -slug (path traversal guard);
# NOW_OVERRIDE (epoch seconds, tests only) pins the clock.
#
# READ-ONLY / NO SECRETS: issues GETs against the merge-queue list
# endpoint, POSTs one boolean. Never enqueues/dequeues, never writes
# ledgers, never admits work, never changes pool caps. Bearer travels in a
# 0600 curl config, never argv (see _gatus_push.sh).
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (see _gatus_push.sh), GH_REPOS
# (owner/repo list, required), MQ_BRANCH (default main),
# MQ_DEPTH_ALERT (default 5), MQ_DEPTH_CLEAR (default 2),
# MQ_WAIT_ALERT_MIN (default 120), MQ_WAIT_CLEAR_MIN (default 60),
# MQ_PAGE_LIMIT (default 10), MQ_REQUIRE_QUEUE (default 0),
# MQ_STATE_FILE (default $HOME/.cache/merge-queue-state.json),
# MQ_KEY_SUFFIX (optional per-source fan-out), NOW_OVERRIDE (tests),
# GH_BIN (default gh).
# Cron (operator): */5 * * * * /opt/status-page/current/ops/case_658bf6b9/push-merge-queue.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gatus_push.sh
. "$HERE/_gatus_push.sh"

DEPTH_ALERT="${MQ_DEPTH_ALERT:-5}"
DEPTH_CLEAR="${MQ_DEPTH_CLEAR:-2}"
WAIT_ALERT="${MQ_WAIT_ALERT_MIN:-120}"
WAIT_CLEAR="${MQ_WAIT_CLEAR_MIN:-60}"
PAGE_LIMIT="${MQ_PAGE_LIMIT:-10}"
REQUIRE_QUEUE="${MQ_REQUIRE_QUEUE:-0}"
BRANCH="${MQ_BRANCH:-main}"
KEY_SUFFIX="${MQ_KEY_SUFFIX:-}"
NOW_PIN="${NOW_OVERRIDE:-}"
GH_BIN="${GH_BIN:-gh}"
STATE_FILE="${MQ_STATE_FILE:-${HOME:-/tmp}/.cache/merge-queue-state.json}"

[[ "$DEPTH_ALERT" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-merge-queue: refusing — MQ_DEPTH_ALERT must be a positive integer" >&2; exit 2; }
[[ "$DEPTH_CLEAR" =~ ^[0-9]+$ ]] \
  || { echo "push-merge-queue: refusing — MQ_DEPTH_CLEAR must be a non-negative integer" >&2; exit 2; }
[[ "$WAIT_ALERT" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-merge-queue: refusing — MQ_WAIT_ALERT_MIN must be a positive integer" >&2; exit 2; }
[[ "$WAIT_CLEAR" =~ ^[0-9]+$ ]] \
  || { echo "push-merge-queue: refusing — MQ_WAIT_CLEAR_MIN must be a non-negative integer" >&2; exit 2; }
[[ "$PAGE_LIMIT" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-merge-queue: refusing — MQ_PAGE_LIMIT must be a positive integer" >&2; exit 2; }
[[ "$REQUIRE_QUEUE" == "0" || "$REQUIRE_QUEUE" == "1" ]] \
  || { echo "push-merge-queue: refusing — MQ_REQUIRE_QUEUE must be 0 or 1" >&2; exit 2; }
[[ "$BRANCH" =~ ^[A-Za-z0-9_.-]+$ ]] \
  || { echo "push-merge-queue: refusing — MQ_BRANCH must be a plain branch name" >&2; exit 2; }
[[ -z "$KEY_SUFFIX" || "$KEY_SUFFIX" =~ ^-[A-Za-z0-9][A-Za-z0-9_-]*$ ]] \
  || { echo "push-merge-queue: refusing — MQ_KEY_SUFFIX must be empty or -slug" >&2; exit 2; }
(( 10#$DEPTH_CLEAR < 10#$DEPTH_ALERT )) \
  || { echo "push-merge-queue: refusing — depth clear band must sit below the alert band" >&2; exit 2; }
(( 10#$WAIT_CLEAR < 10#$WAIT_ALERT )) \
  || { echo "push-merge-queue: refusing — wait clear band must sit below the alert band" >&2; exit 2; }
read -r -a REPOS <<<"${GH_REPOS:-}"
(( ${#REPOS[@]} > 0 )) \
  || { echo "push-merge-queue: refusing — GH_REPOS must be set" >&2; exit 2; }
for repo in "${REPOS[@]}"; do
  # GitHub owners carry no dots; `.`/`..` as a name would walk the API path.
  [[ "$repo" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$ && "${repo#*/}" != "." && "${repo#*/}" != ".." ]] \
    || { echo "push-merge-queue: refusing — bad repo '$repo' (want owner/repo)" >&2; exit 2; }
done

[[ -n "${GATUS_URL:-}" ]] \
  || { echo "push-merge-queue: refusing — GATUS_URL is unset" >&2; exit 2; }
[[ -n "${GATUS_EXTERNAL_TOKEN:-}" ]] \
  || { echo "push-merge-queue: refusing — GATUS_EXTERNAL_TOKEN is unset" >&2; exit 2; }

command -v "$GH_BIN" >/dev/null 2>&1 \
  || { echo "push-merge-queue: refusing — no '$GH_BIN' on PATH" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 \
  || { echo "push-merge-queue: refusing — python3 required" >&2; exit 2; }

READY_KEY="platform_merge-queue${KEY_SUFFIX}"

fail_push() { gatus_push "$READY_KEY" false "$1" || true; exit 1; }

# Evaluate every repo in one python3 pass (stdlib only). The gh shim is a
# subprocess of python (`--gh-bin`), so stub fixtures drive the whole read.
# Stdout is one "<STATE>\t<true|false>\t<message>" control line followed by
# the case_401f7b0d-merge-queue-v1 verdict JSON line.
EVAL="$(NOW_OVERRIDE="$NOW_PIN" python3 - "$STATE_FILE" "$DEPTH_ALERT" "$DEPTH_CLEAR" "$WAIT_ALERT" "$WAIT_CLEAR" "$PAGE_LIMIT" "$REQUIRE_QUEUE" "$BRANCH" "$GH_BIN" "${REPOS[@]}" <<'PY'
import json, os, subprocess, sys, time

state_path, depth_alert_s, depth_clear_s, wait_alert_s, wait_clear_s = sys.argv[1:6]
page_limit_s, require_s, branch, gh_bin, repos = sys.argv[6], sys.argv[7], sys.argv[8], sys.argv[9], sys.argv[10:]
depth_alert, depth_clear = int(depth_alert_s), int(depth_clear_s)
wait_alert, wait_clear = int(wait_alert_s), int(wait_clear_s)
page_limit, require_queue = int(page_limit_s), require_s == "1"
now_pin = os.environ.get("NOW_OVERRIDE") or ""
now = int(now_pin) if now_pin.strip().isdigit() else int(time.time())

def parse_iso(ts):
    # GitHub serves RFC3339; fromisoformat takes it after the Z fix. An
    # unparseable stamp counts toward depth, never toward wait.
    try:
        if not isinstance(ts, str) or not ts:
            return None
        return int(__import__("datetime").datetime.fromisoformat(
            ts.replace("Z", "+00:00")).timestamp())
    except (ValueError, OverflowError):
        return None

queue_absent_everywhere = True
depth, oldest_wait, oldest_repo = 0, None, ""
detail_bits = []
try:
    for repo in repos:
        entries = []
        present, page = False, 1
        while True:
            if page > page_limit:
                raise ValueError(f"queue deeper than MQ_PAGE_LIMIT={page_limit} for {repo}; oldest entries unseen")
            proc = subprocess.run(
                [gh_bin, "api", f"repos/{repo}/merge-queue/{branch}?per_page=100&page={page}"],
                capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                # A 404 on page 1 means no queue configured (today: every
                # repo — the case_082e9f7b trigger is inert until the rule
                # exists). A 404 past page 1 is transport corruption: the
                # queue existed a page ago.
                if page == 1 and "Not Found" in (proc.stderr or ""):
                    break
                raise ValueError(f"gh merge-queue read failed for {repo} (page {page})")
            present = True
            try:
                root = json.loads(proc.stdout or "null")
            except ValueError:
                raise ValueError(f"unparseable merge-queue response for {repo} (page {page})")
            # The list endpoint answers an object with an `entries` array; a
            # bare array or any other shape is corrupt, never an empty queue.
            if not isinstance(root, dict) or not isinstance(root.get("entries"), list):
                raise ValueError(f"unexpected merge-queue shape for {repo} (page {page}); want object with entries[]")
            batch = root["entries"]
            entries.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        if present:
            queue_absent_everywhere = False
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"unexpected merge-queue entry shape for {repo}; want object")
            depth += 1
            enq = parse_iso(entry.get("enqueuedAt"))
            if enq is not None:
                wait = (now - enq) // 60
                if wait >= 0 and (oldest_wait is None or wait > oldest_wait):
                    oldest_wait, oldest_repo = wait, repo
        detail_bits.append((repo, len(entries)))
except ValueError as exc:
    print(f"STATE\tfalse\tmerge queue unreadable ({exc}); fail-closed DOWN")
    print(json.dumps({"schema": "case_401f7b0d-merge-queue-v1",
                      "verdict": {"state": "unreadable", "depthEntries": None,
                                  "oldestWaitMinutes": None, "note": str(exc)}}))
    sys.exit(0)

if oldest_wait is None:
    wait_txt = "wait unknown"
else:
    wait_txt = f"oldest wait {oldest_wait}m in {oldest_repo}"
per_repo = ", ".join(f"{r}:{n}" for r, n in detail_bits) or "no repos answered"

trips_alert = depth >= depth_alert or (oldest_wait is not None and oldest_wait >= wait_alert)
clears = depth <= depth_clear and (oldest_wait is None or oldest_wait <= wait_clear)

# Hysteresis latch: between the bands the previous verdict holds. An
# unreadable state file degrades to stateless (alert still pages), never
# to silent.
latched = None
try:
    with open(state_path, encoding="utf-8") as h:
        prior = json.load(h)
    if isinstance(prior, dict) and prior.get("state") in ("alert", "clear"):
        latched = prior["state"]
except (OSError, ValueError):
    latched = None

if trips_alert:
    state, ok, note = "alert", False, f"merge queue saturated: depth {depth} (alert {depth_alert}), {wait_txt} (alert {wait_alert}m); per-repo {per_repo}"
elif clears:
    state, ok, note = "clear", True, f"merge queue clear: depth {depth} (clear {depth_clear}), {wait_txt} (clear {wait_clear}m); per-repo {per_repo}"
elif latched == "alert":
    state, ok, note = "alert", False, f"merge queue still alert (latch holds inside the bands): depth {depth} (clear {depth_clear}), {wait_txt} (clear {wait_clear}m); per-repo {per_repo}"
else:
    state, ok, note = "clear", True, f"merge queue inside the bands, no prior alert: depth {depth}, {wait_txt}; per-repo {per_repo}"

if queue_absent_everywhere:
    if require_queue:
        state, ok = "alert", False
        note = f"merge queue required but absent on every repo ({per_repo}); fail-closed DOWN"
    else:
        state, ok = "clear", True
        note = f"merge queue not enabled on any repo ({per_repo}); nothing to saturate"

try:
    parent = os.path.dirname(os.path.abspath(state_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as h:
        json.dump({"state": state, "depthEntries": depth, "oldestWaitMinutes": oldest_wait}, h)
except OSError:
    pass  # state is a flap guard, never the verdict: a stuck latch degrades to stateless next run

print(f"STATE\t{str(ok).lower()}\t{note}")
print(json.dumps({"schema": "case_401f7b0d-merge-queue-v1",
                  "verdict": {"state": state, "depthEntries": depth,
                              "oldestWaitMinutes": oldest_wait, "note": note}}))
PY
)" || {
  echo "push-merge-queue: evaluator failed — fail-closed, marking endpoint DOWN" >&2
  gatus_push "$READY_KEY" false "evaluator failure (fail-closed DOWN)" || true
  exit 1
}

# Split the control line from the verdict JSON (the single {-leading line).
VERDICT="$(printf '%s\n' "$EVAL" | grep '^{' | tail -n 1)"
CTRL="$(printf '%s\n' "$EVAL" | grep -v '^{' || true)"
STATE_LINE="$(grep '^STATE' <<<"$CTRL" | tail -n 1)"
[[ -n "$STATE_LINE" && -n "$VERDICT" ]] || {
  echo "push-merge-queue: evaluator output incomplete — fail-closed, marking endpoint DOWN" >&2
  gatus_push "$READY_KEY" false "evaluator output incomplete (fail-closed DOWN)" || true
  exit 1
}

echo "push-merge-queue: $VERDICT"  # verdict lands in the cron log, not just the push
state_body="${STATE_LINE#*$'\t'}"
state_success="${state_body%%$'\t'*}"; state_msg="${state_body#*$'\t'}"

rc=0
if [[ "$state_success" == "true" ]]; then
  gatus_push "$READY_KEY" true || rc=1
else
  gatus_push "$READY_KEY" false "$state_msg" || true
  rc=1  # a DOWN endpoint is an incident: cron must report it
fi
exit "$rc"
