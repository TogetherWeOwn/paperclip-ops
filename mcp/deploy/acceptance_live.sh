#!/usr/bin/env bash
# case_961e6ec2 — runbook step 9, as a script, run ON THE VPS by the operator.
#
# WHY THIS EXISTS
# ---------------
# `acceptance_transport.sh` (case_adeb99c9) drives the whole request loop through the
# real server over real HTTP and is green. It cannot reach three layers that
# only exist on the VPS: Caddy, the Paperclip tool gateway stamping
# `x-paperclip-agent-id`, and `requireLiveRun` corroborating against
# `heartbeat_runs`. This script makes the same assertions against the live path.
#
# WHY IT IS A SCRIPT AND NOT FOUR PASTED CURLS
# --------------------------------------------
# Step 9 as originally written asserts a `403` on a call carrying NO identity
# headers at all. That call is refused three times over — a missing agent id, a
# missing company id and a missing run id all raise `identity_header_missing`,
# the same code — so the assertion holds even if the agent-id check is deleted
# outright, or silently defaults to a principal. That exact hole was found by
# mutation in the container suite. Section 3 below omits EXACTLY ONE input, and
# discriminates on the message rather than the code, because the code cannot.
#
# Everything negative here is attributable only because section 2 first proves
# the same call is ADMITTED with all inputs present. If the baseline is red the
# script stops: a refusal that would have happened anyway proves nothing.
#
# WHAT IT STILL CANNOT DO, AND WHO CAN
# ------------------------------------
#   * Section 5 proves the requester is the AUTHENTICATED caller. Run from the
#     VPS the authenticated caller is whoever the operator names in the header,
#     so this proves the SERVER binds the row to the header. That the header is
#     AUTHENTIC — the gateway's half — can only be shown by an agent calling the
#     tool through its own gateway session, which no shell on the VPS can fake.
#     Section 5 prints the exact hand-off for that; see also section 9.
#   * Section 8 (Caddy's `remote_ip` source restriction) must run from a
#     DIFFERENT host. Invoked here it is reported as SKIP, never as PASS.
#
# Every section prints PASS / FAIL / SKIP with a reason. Exit status is 0 only
# when there are no FAILs and no SKIPs other than the two that are structurally
# impossible from this box, which are named individually in the summary.
#
# USAGE
#   sudo -v                                  # prime the sudo timestamp
#   ./acceptance_live.sh --url https://MCP_HOSTNAME/mcp
#
# SEAMS (defaults are the real install; the self-test overrides them)
#   ORG_MCP_URL         endpoint, same as --url
#   ORG_MCP_ENV_FILE    default /etc/caddy/org-request-mcp.env
#   ORG_MCP_CONFIG      default /etc/org-request-mcp/config.json
#   ORG_MCP_SUDO        default "sudo"; set empty to read the files directly
#   ORG_MCP_ENGINE      default auto: $(command -v docker || command -v podman)
#                       (case_d8f38556: node7b1ff338 is Docker-only, so a hard-coded podman
#                       fails the heartbeat_runs control query here too)
#   ORG_MCP_PODMAN      legacy alias for ORG_MCP_ENGINE; honoured when the
#                       former is unset (the self-test still uses it)
#   ORG_MCP_TEMPLATE    default E4_AUDIT_ANALYST
set -uo pipefail

URL="${ORG_MCP_URL:-}"
ENV_FILE="${ORG_MCP_ENV_FILE:-/etc/caddy/org-request-mcp.env}"
CONFIG="${ORG_MCP_CONFIG:-/etc/org-request-mcp/config.json}"
SUDO="${ORG_MCP_SUDO-sudo}"
ENGINE="${ORG_MCP_ENGINE:-${ORG_MCP_PODMAN:-$(command -v docker || command -v podman)}}"
TEMPLATE="${ORG_MCP_TEMPLATE:-E4_AUDIT_ANALYST}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url) URL="${2:-}"; shift 2 ;;
    --template) TEMPLATE="${2:-}"; shift 2 ;;
    -h|--help) sed -n '1,50p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

PASSES=0; FAILS=0; SKIPS=0
declare -a FAILED_SECTIONS=() SKIPPED_SECTIONS=()
pass() { PASSES=$((PASSES+1)); printf 'PASS  %s\n' "$*"; }
fail() { FAILS=$((FAILS+1)); FAILED_SECTIONS+=("$CURRENT"); printf 'FAIL  %s\n' "$*"; }
skip() { SKIPS=$((SKIPS+1)); SKIPPED_SECTIONS+=("$CURRENT"); printf 'SKIP  %s\n' "$*"; }
CURRENT="(none)"
section() { CURRENT="$1"; printf '\n=== %s ===\n' "$1"; }
die() { printf '\nABORT: %s\n' "$*" >&2; exit 70; }

[[ -n "$URL" ]] || die "no endpoint. Pass --url https://MCP_HOSTNAME/mcp"
for tool in curl jq sha256sum; do
  command -v "$tool" >/dev/null || die "$tool is required"
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
chmod 700 "$TMP"

# --- credential handling ----------------------------------------------------
# The bearer never becomes an argument of anything. `curl -K -` reads the
# Authorization header from a config file on STDIN, which is the same rule the
# runbook applies to its own step 9 (case_0409633c): /proc/*/cmdline is world-readable
# and every company on this box shares the host.
read_env_file() { $SUDO cat "$ENV_FILE"; }
curl_auth_config() {
  read_env_file | sed -n 's/^ORG_MCP_BEARER=\(.*\)$/header = "Authorization: Bearer \1"/p'
}
if [[ -z "$(curl_auth_config)" ]]; then
  die "no ORG_MCP_BEARER= line in $ENV_FILE (is EnvironmentFile= wired up? step 5)"
fi

# --- configuration under test -----------------------------------------------
CONFIG_JSON="$TMP/config.json"
$SUDO cat "$CONFIG" > "$CONFIG_JSON" 2>/dev/null || die "cannot read $CONFIG"
jq -e . "$CONFIG_JSON" >/dev/null 2>&1 || die "$CONFIG is not valid JSON"
CFG_COMPANY="$(jq -r '.companyId // empty' "$CONFIG_JSON")"
CFG_QUEUE_SCRIPT="$(jq -r '.queueScript // empty' "$CONFIG_JSON")"
CFG_DB_CTR="$(jq -r '.dbContainer // "paperclip-db"' "$CONFIG_JSON")"
[[ -n "$CFG_COMPANY" ]] || die "$CONFIG has no companyId"
[[ -n "$CFG_QUEUE_SCRIPT" ]] || die "$CONFIG has no queueScript"

# The queue file the transport will append to. org_request_queue.sh resolves
# QUEUE relative to its own directory unless the environment overrides it, and
# queueEnv is the only thing that can override it here.
QUEUE_FILE="$(jq -r '.queueEnv.QUEUE // empty' "$CONFIG_JSON")"
[[ -n "$QUEUE_FILE" ]] || QUEUE_FILE="$(dirname "$CFG_QUEUE_SCRIPT")/provisioner-request-queue.jsonl"

queue_lines() {
  # A queue file that does not exist yet is zero lines, not an error: the very
  # first submit on a fresh install creates it.
  $SUDO test -e "$QUEUE_FILE" 2>/dev/null || { echo 0; return; }
  $SUDO wc -l < "$QUEUE_FILE" 2>/dev/null | tr -d ' ' || echo 0
}

# --- HTTP -------------------------------------------------------------------
# Body to a FILE, status code to stdout. Writing `-w '%{http_code}'` into the
# same stream corrupts the JSON parse and the failure then reads as a server
# bug rather than a harness bug.
BODY="$TMP/body.json"
# rpc <json-body> <header...> -> prints the status code, body lands in $BODY
rpc() {
  local body="$1"; shift
  local -a hdrs=()
  local h; for h in "$@"; do hdrs+=(-H "$h"); done
  curl -s -K <(curl_auth_config) -o "$BODY" -w '%{http_code}' -X POST \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    "${hdrs[@]}" -d "$body" "$URL"
}
submit_body() {
  # $1 = template, $2 = title, $3 = optional extra arguments object to merge
  local extra="${3:-}"
  [[ -n "$extra" ]] || extra='{}'
  jq -nc --arg t "$1" --arg title "$2" --argjson extra "$extra" \
    '{jsonrpc:"2.0",id:1,method:"tools/call",
      params:{name:"submit_provisioning_request",
              arguments:({template:$t,title:$title} + $extra)}}'
}
body_error_code() { jq -r '.error.data.code // .error.code // empty' "$BODY" 2>/dev/null; }
body_error_message() { jq -r '.error.data.message // .error.message // empty' "$BODY" 2>/dev/null; }
tool_text() { jq -r '.result.content[0].text // empty' "$BODY" 2>/dev/null; }

# ---------------------------------------------------------------------------
section "1. a live principal to test with"
# ---------------------------------------------------------------------------
# Every negative below needs a positive control, and with requireLiveRun ON the
# control needs an (agent, run, company) triple the database will actually
# corroborate. Inventing one makes the baseline fail for the wrong reason and
# every later refusal unattributable. So: ask the database for a real one.
db_query() {
  printf '%s' "$1" | $ENGINE exec -i "$CFG_DB_CTR" sh -c \
    'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atq -v ON_ERROR_STOP=1 -f -' 2>"$TMP/db.err"
}
LIVE_ROW="$(db_query "SELECT id || ' ' || agent_id FROM heartbeat_runs
  WHERE company_id = '$CFG_COMPANY'::uuid AND status = 'running'
  ORDER BY started_at DESC LIMIT 1;")"
if [[ -z "${LIVE_ROW// }" ]]; then
  if [[ -s "$TMP/db.err" ]]; then
    die "cannot query heartbeat_runs via '$ENGINE exec $CFG_DB_CTR': $(head -1 "$TMP/db.err")"
  fi
  die "no RUNNING heartbeat run for company $CFG_COMPANY.
  Wake any agent in this company and re-run: with requireLiveRun ON there is no
  positive control without one, and a suite with no positive control is a suite
  that cannot attribute a single refusal below."
fi
LIVE_RUN="${LIVE_ROW%% *}"; LIVE_AGENT="${LIVE_ROW##* }"
printf 'live principal: agent=%s run=%s\n' "$LIVE_AGENT" "$LIVE_RUN"
pass "corroboratable (agent, run, company) triple obtained from heartbeat_runs"

ID_HDRS=(
  "x-paperclip-agent-id: $LIVE_AGENT"
  "x-paperclip-company-id: $CFG_COMPANY"
  "x-paperclip-run-id: $LIVE_RUN"
  "x-paperclip-correlation-id: case_961e6ec2-live-acceptance"
)

# ---------------------------------------------------------------------------
section "2. BASELINE — the same call is admitted with every input present"
# ---------------------------------------------------------------------------
# "Admitted" is the property that matters, and it is NOT "the request was
# created". The queue may legitimately refuse this agent this template on
# authorization grounds, and that refusal arrives as HTTP 200 with a REFUSED
# body — it means the transport let the call through, which is exactly what the
# baseline has to establish. A 401/403/503 means it did not.
BASE_QUEUE_BEFORE="$(queue_lines)"
CODE="$(rpc "$(submit_body "$TEMPLATE" "case_961e6ec2 baseline")" "${ID_HDRS[@]}")"
if [[ "$CODE" == "200" ]]; then
  pass "control call admitted (HTTP 200); transport-level answer: $(tool_text | head -c 120)"
else
  printf '  status=%s code=%s message=%s\n' "$CODE" "$(body_error_code)" "$(body_error_message)"
  fail "control call was refused at the transport (HTTP $CODE). Every assertion below
      would be a refusal that happened anyway. Fix this first — most likely the
      run corroboration (503 run_check_unavailable = the container engine
      blocked by the unit sandbox, runbook step 7) or a wrong companyId in $CONFIG."
  echo; echo "SUMMARY: baseline red, stopping. passes=$PASSES fails=$FAILS"
  exit 1
fi
BASE_QUEUE_AFTER="$(queue_lines)"

# ---------------------------------------------------------------------------
section "3. a call missing ONLY the agent header is refused, for that reason"
# ---------------------------------------------------------------------------
# Exactly one input is removed. Company and run stay present and valid, so the
# company check and the run check both pass and cannot account for the refusal.
#
# The CODE is not the assertion. `identity_header_missing` is raised by the
# missing-agent, missing-company and missing-run arms alike, and a mutation that
# defaults a missing agent id to some principal still yields that code from a
# later arm. The message is the only thing that discriminates, so assert on it.
CODE="$(rpc "$(submit_body "$TEMPLATE" "case_961e6ec2 no agent header")" \
  "x-paperclip-company-id: $CFG_COMPANY" \
  "x-paperclip-run-id: $LIVE_RUN" \
  "x-paperclip-correlation-id: case_961e6ec2-live-acceptance")"
MSG="$(body_error_message)"
if [[ "$CODE" != "403" ]]; then
  printf '  status=%s body=%s\n' "$CODE" "$(head -c 200 "$BODY")"
  fail "expected 403 with only the agent header absent; got $CODE.
      A 200 here means a missing agent id resolved to SOME principal — the
      transport acted anonymously under an assumed name."
elif [[ "$MSG" != *"x-paperclip-agent-id"* ]]; then
  fail "403, but the message does not name x-paperclip-agent-id, so this refusal
      is not the agent-identity control: $MSG"
elif [[ "$MSG" != *"headerPolicy.metadata.forward"* ]]; then
  fail "403 naming the agent header, but the message no longer names
      headerPolicy.metadata.forward — the operator is not told the fix: $MSG"
else
  pass "403 identity refusal attributable to the agent header alone, and it names the fix"
fi

# ---------------------------------------------------------------------------
section "4. a 'requester' argument is refused AND appends nothing"
# ---------------------------------------------------------------------------
# The refusal lives in buildQueueArgs, which runs AFTER identity and after run
# corroboration. Sending this without valid headers would be refused earlier and
# the line-count assertion would then hold for a reason that has nothing to do
# with argument handling — so this call carries the full, live identity.
SPOOF="$(cat /proc/sys/kernel/random/uuid)"
QUEUE_BEFORE="$(queue_lines)"
CODE="$(rpc "$(submit_body "$TEMPLATE" "case_961e6ec2 spoofed requester" \
  "$(jq -nc --arg r "$SPOOF" '{requester:$r}')")" "${ID_HDRS[@]}")"
TEXT="$(tool_text)"
QUEUE_AFTER="$(queue_lines)"
if [[ "$TEXT" != *"identity_argument_refused"* ]]; then
  printf '  status=%s body=%s\n' "$CODE" "$(head -c 300 "$BODY")"
  fail "a 'requester' argument was not refused as identity_argument_refused"
elif [[ "$QUEUE_AFTER" != "$QUEUE_BEFORE" ]]; then
  fail "refused, but the queue grew from $QUEUE_BEFORE to $QUEUE_AFTER lines.
      A refusal that still writes is not a refusal."
else
  pass "REFUSED (identity_argument_refused) and the queue is unchanged at $QUEUE_AFTER lines"
fi
# The spoofed id must not appear anywhere in the queue, which is the assertion
# a line count alone cannot make: a write that REPLACED a line keeps the count.
if $SUDO grep -qF "$SPOOF" "$QUEUE_FILE" 2>/dev/null; then
  fail "the spoofed requester id $SPOOF is present in $QUEUE_FILE"
else
  pass "the spoofed requester id appears nowhere in the queue"
fi

# ---------------------------------------------------------------------------
section "5. the row's requester is the authenticated caller"
# ---------------------------------------------------------------------------
# Two ways to see it, and both prove the same binding — the transport writes
# --requester from the header and from nothing else:
#   (a) the baseline submit created a row: its requester is the header agent.
#   (b) the queue refused the baseline on authorization grounds: the refusal
#       names the header agent as the requester it was asked about.
#
# Read the row ONLY if the baseline actually grew the queue. Reading `tail -1`
# unconditionally would assert against whatever row happened to be there
# already — most likely one this run never created — and that passes or fails
# for reasons that have nothing to do with this install.
ROW_REQUESTER=""
if ((BASE_QUEUE_AFTER > BASE_QUEUE_BEFORE)); then
  LAST_ROW="$($SUDO tail -n 1 "$QUEUE_FILE" 2>/dev/null || true)"
  ROW_REQUESTER="$(jq -r '.requester // empty' <<<"$LAST_ROW" 2>/dev/null || true)"
fi
if [[ -n "$ROW_REQUESTER" && "$ROW_REQUESTER" == "$LIVE_AGENT" ]]; then
  pass "the request the baseline created records requester=$ROW_REQUESTER, the authenticated caller"
elif [[ -n "$ROW_REQUESTER" ]]; then
  fail "the row the baseline created records requester=$ROW_REQUESTER but the
      authenticated caller was $LIVE_AGENT. STOP — this is the failure the epic
      exists to prevent."
else
  skip "the baseline submit did not create a row (the queue refused it on
      authorization grounds, which is a legitimate answer). The binding is still
      shown by section 4. To close this one, re-run with --template set to a
      template agent $LIVE_AGENT may actually submit, or run the gateway hand-off
      in section 9 — which is in any case the only thing that proves the header
      itself is authentic."
fi

# ---------------------------------------------------------------------------
section "6. requireLiveRun is ON, and a dead run is refused"
# ---------------------------------------------------------------------------
if [[ "$(jq -r '.requireLiveRun' "$CONFIG_JSON")" == "false" ]]; then
  fail "$CONFIG sets requireLiveRun:false. The deployed transport is trusting the
      identity header without asking the database to agree. That setting is
      sanctioned only for the offline container suite."
else
  pass "$CONFIG does not disable requireLiveRun"
fi
# Behaviour, not just configuration: a syntactically perfect identity whose run
# id belongs to no RUNNING row. Same agent, same company — one input changed.
DEAD_RUN="$(cat /proc/sys/kernel/random/uuid)"
CODE="$(rpc "$(submit_body "$TEMPLATE" "case_961e6ec2 dead run")" \
  "x-paperclip-agent-id: $LIVE_AGENT" \
  "x-paperclip-company-id: $CFG_COMPANY" \
  "x-paperclip-run-id: $DEAD_RUN" \
  "x-paperclip-correlation-id: case_961e6ec2-live-acceptance")"
ECODE="$(body_error_code)"
if [[ "$CODE" == "403" && "$ECODE" == "run_not_live" ]]; then
  pass "403 run_not_live — the database, not the header, decided this"
elif [[ "$CODE" == "503" && "$ECODE" == "run_check_unavailable" ]]; then
  fail "503 run_check_unavailable. This IS fail-closed, but for the wrong reason:
      the server cannot reach $CFG_DB_CTR, so the corroboration never ran and
      this section proves nothing. See runbook step 7 — comment out the unit's
      IPAddressDeny/IPAddressAllow lines and re-run."
else
  printf '  status=%s code=%s\n' "$CODE" "$ECODE"
  fail "a run id with no RUNNING heartbeat row was not refused (got $CODE/$ECODE).
      requireLiveRun is not actually corroborating."
fi

# ---------------------------------------------------------------------------
section "7. no bearer, no service — and no WWW-Authenticate on the 401"
# ---------------------------------------------------------------------------
HDR_DUMP="$TMP/headers.txt"
CODE="$(curl -s -D "$HDR_DUMP" -o "$BODY" -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' "$URL")"
if [[ "$CODE" != "401" ]]; then
  fail "expected 401 without a bearer; got $CODE"
elif grep -qi '^www-authenticate:' "$HDR_DUMP"; then
  fail "the 401 carries WWW-Authenticate. The gateway reads that as 'go discover
      OAuth endpoints' and a wrong-bearer error gets buried under a protocol error."
else
  pass "401 without a bearer, and no WWW-Authenticate header"
fi

# ---------------------------------------------------------------------------
section "8. Caddy's source restriction"
# ---------------------------------------------------------------------------
skip "runbook step 9(3) must run from a DIFFERENT host — from this box the
      remote_ip matcher cannot be exercised at all, and a PASS here would be a
      lie. Run, from anywhere else:
        curl -s -o /dev/null -w '%{http_code}\\n' $URL -H 'Authorization: Bearer <bearer>' -d '{}'
      EXPECT 403 (Caddy), not 401 and not 200."

# ---------------------------------------------------------------------------
section "9. the gateway's half — hand-off, not an assertion"
# ---------------------------------------------------------------------------
skip "this script sets the identity header itself, so it proves the SERVER binds
      the row to the header, never that the header is AUTHENTIC. That needs an
      agent calling submit_provisioning_request through its own gateway session
      after the tool_connection is registered with
      config.headerPolicy.metadata.forward = [agent_id, company_id, run_id,
      correlation_id]. Ask that agent to submit with NO requester argument, then
      check that the newest queue row's requester is that agent's own uuid:
        sudo tail -n 1 $QUEUE_FILE | jq -r .requester"

# ---------------------------------------------------------------------------
echo
echo "=== SUMMARY ==="
printf 'passes=%d fails=%d skips=%d\n' "$PASSES" "$FAILS" "$SKIPS"
if ((FAILS > 0)); then
  printf 'failed: %s\n' "${FAILED_SECTIONS[*]}"
  echo "RESULT: step 9 NOT satisfied."
  exit 1
fi
# Sections 8 and 9 are structurally impossible from this host; any OTHER skip is
# a hole in the evidence and must not read as a pass.
UNEXPECTED=0
for s in "${SKIPPED_SECTIONS[@]:-}"; do
  case "$s" in
    "8. Caddy's source restriction"|"9. the gateway's half — hand-off, not an assertion") ;;
    "") ;;
    *) UNEXPECTED=1; printf 'unexpected skip: %s\n' "$s" ;;
  esac
done
if ((UNEXPECTED)); then
  echo "RESULT: no failures, but an assertion did not run. Step 9 NOT satisfied."
  exit 1
fi
echo "RESULT: every assertion this host can make is green."
echo "Still owed, from elsewhere: section 8 (another host) and section 9 (an agent)."
exit 0
