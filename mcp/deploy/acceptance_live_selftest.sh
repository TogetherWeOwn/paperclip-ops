#!/usr/bin/env bash
# case_961e6ec2 — prove acceptance_live.sh is not vacuous, from inside a container.
#
# acceptance_live.sh can only run on the VPS. That means the operator is the
# first person who will ever execute it, against a freshly installed transport,
# with no way to tell "these assertions passed" from "these assertions never
# actually looked at anything". A green suite nobody has seen go red is not
# evidence. So this runs first, here.
#
# WHAT IS REAL AND WHAT IS SUBSTITUTED
# ------------------------------------
#   REAL: mcp/org-request-mcp.mjs, unmodified, on a real TCP socket, driven by
#         acceptance_live.sh over real HTTP with real headers. requireLiveRun is
#         ON — the same setting the install ships — so the corroboration code
#         path executes rather than being skipped.
#   SUBSTITUTED: `podman` and `sudo`, through the seams acceptance_live.sh
#         already exposes, and the queue script. The fake podman answers the
#         same two queries the real one would, from argv/env/stdin, and answers
#         them the way the database would. It is substituted, not disabled:
#         a stub that always said yes would make section 6 pass by construction,
#         which is the failure this whole file exists to catch.
#   NOT COVERED: Caddy, TLS, the source restriction and the gateway. Those are
#         sections 8 and 9 of acceptance_live.sh and they are SKIPs there too.
#
# THE ACTUAL TEST
# ---------------
#   Phase 1  baseline: unmutated server, acceptance_live.sh must exit 0 with
#            zero unexpected skips. Without this every red below is
#            unattributable — the suite could be failing for a reason that has
#            nothing to do with the mutation.
#   Phase 2  four mutations, each removing ONE control from a COPY of the
#            server. Each must exit non-zero AND the named section must be the
#            one that failed. "The suite went red" is not enough: deleting the
#            assertions would also make it red on the right exit code.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
SERVER_SRC="$REPO/mcp/org-request-mcp.mjs"
SUITE="$HERE/acceptance_live.sh"
[[ -f "$SERVER_SRC" ]] || { echo "missing $SERVER_SRC" >&2; exit 1; }
[[ -x "$SUITE" || -f "$SUITE" ]] || { echo "missing $SUITE" >&2; exit 1; }
command -v node >/dev/null || { echo "node 18+ required" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq required" >&2; exit 1; }

TMP="$(mktemp -d "${PAPERCLIP_RUN_SCRATCH_DIR:-/tmp}/case_710f0d83.XXXXXX")"
chmod 700 "$TMP"
SERVER_PID=""
cleanup() { [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null; rm -rf "$TMP"; }
trap cleanup EXIT

# --- the identities the fake database will vouch for ------------------------
COMPANY="$(cat /proc/sys/kernel/random/uuid)"
LIVE_AGENT="$(cat /proc/sys/kernel/random/uuid)"
LIVE_RUN="$(cat /proc/sys/kernel/random/uuid)"

mkdir -p "$TMP/bin"

# A `podman` that answers exactly the two queries the suite and the server ask,
# and answers them from the same inputs the real one gets: stdin for the SQL,
# the environment for the server's parameters. Anything else is an error, so a
# third query added later cannot silently return the empty string and read as
# "no matching row".
cat > "$TMP/bin/podman" <<PODMAN
#!/usr/bin/env bash
set -uo pipefail
sql="\$(cat)"
case "\$sql" in
  *"ORDER BY started_at"*)
    # acceptance_live.sh asking for any RUNNING run in this company.
    case "\$sql" in
      *"$COMPANY"*) printf '%s %s\n' "$LIVE_RUN" "$LIVE_AGENT" ;;
      *) : ;;
    esac
    ;;
  *"heartbeat_runs"*)
    # The server corroborating one specific (run, agent, company) triple.
    if [[ "\${PGV_RUN_ID:-}" == "$LIVE_RUN" \
       && "\${PGV_AGENT_ID:-}" == "$LIVE_AGENT" \
       && "\${PGV_COMPANY_ID:-}" == "$COMPANY" ]]; then
      printf '1\n'
    fi
    ;;
  *)
    echo "fake podman: unrecognised query" >&2; exit 3 ;;
esac
exit 0
PODMAN
chmod +x "$TMP/bin/podman"

# The queue. Named org_request_queue.sh because the server refuses to start
# otherwise, and that refusal is a feature. It records what the transport passed
# as --requester, which is the only thing sections 4 and 5 read.
QUEUE_FILE="$TMP/provisioner-request-queue.jsonl"
cat > "$TMP/org_request_queue.sh" <<'QUEUE'
#!/usr/bin/env bash
set -uo pipefail
sub="${1:-}"; shift || true
requester=""; template=""; title=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --requester) requester="${2:-}"; shift 2 ;;
    --template)  template="${2:-}";  shift 2 ;;
    --title)     title="${2:-}";     shift 2 ;;
    *) shift ;;
  esac
done
[[ "$sub" == "submit" ]] || { echo "REFUSED: unsupported"; exit 2; }
# Phase 4's hook: a template this requester may not submit. The queue answering
# "no" is a legitimate outcome that creates no row, which is exactly the case in
# which section 5 has nothing to read.
[[ "$template" == "E9_REFUSED" ]] && { echo "REFUSED: $requester may not submit $template" >&2; exit 2; }
jq -nc --arg r "$requester" --arg t "$template" --arg ti "$title" \
  '{request_id:"REQ-TEST",requester:$r,template:$t,title:$ti,status:"PENDING"}' \
  >> "$QUEUE"
echo "created REQ-TEST for $requester"
QUEUE
chmod +x "$TMP/org_request_queue.sh"

BEARER="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
BEARER_SHA="$(printf '%s' "$BEARER" | sha256sum | cut -d' ' -f1)"
ENV_FILE="$TMP/org-request-mcp.env"
printf 'ORG_MCP_BEARER=%s\n' "$BEARER" > "$ENV_FILE"
chmod 600 "$ENV_FILE"

PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
CONFIG="$TMP/config.json"
write_config() {
  jq -n --arg cid "$COMPANY" --arg sha "$BEARER_SHA" --arg q "$TMP/org_request_queue.sh" \
        --argjson port "$PORT" --arg queue "$QUEUE_FILE" '{
    companyId: $cid, bearerSha256: $sha, queueScript: $q, port: $port,
    dbContainer: "paperclip-db-fake",
    requireLiveRun: true,
    queueEnv: { QUEUE: $queue }
  }' > "$CONFIG"
  chmod 600 "$CONFIG"   # the server refuses any mode with group or other bits
}
write_config

start_server() { # start_server <path-to-server.mjs>
  local src="$1"
  PATH="$TMP/bin:$PATH" node "$src" "$CONFIG" > "$TMP/server.log" 2>&1 &
  SERVER_PID=$!
  local i
  for i in $(seq 1 50); do
    if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/mcp" -d '{}' 2>/dev/null; then return 0; fi
    kill -0 "$SERVER_PID" 2>/dev/null || { echo "server died:"; cat "$TMP/server.log"; return 1; }
    sleep 0.1
  done
  echo "server never came up:"; cat "$TMP/server.log"; return 1
}
stop_server() { [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null; SERVER_PID=""; }

run_suite() { # -> exit status; output in $TMP/suite.out
  : > "$QUEUE_FILE"
  ORG_MCP_URL="http://127.0.0.1:$PORT/mcp" \
  ORG_MCP_ENV_FILE="$ENV_FILE" \
  ORG_MCP_CONFIG="$CONFIG" \
  ORG_MCP_SUDO="" \
  ORG_MCP_PODMAN="$TMP/bin/podman" \
  ORG_MCP_TEMPLATE="${SUITE_TEMPLATE:-E4_AUDIT_ANALYST}" \
  bash "$SUITE" > "$TMP/suite.out" 2>&1
  echo $?
}

RESULT=0
note() { printf '\n---- %s\n' "$*"; }

# ===========================================================================
note "PHASE 1 — baseline: the unmutated server must go green"
# ===========================================================================
start_server "$SERVER_SRC" || exit 1
STATUS="$(run_suite)"
stop_server
if [[ "$STATUS" != "0" ]]; then
  echo "BASELINE RED (exit $STATUS). Every mutation below would be unattributable."
  cat "$TMP/suite.out"
  exit 1
fi
if grep -q '^unexpected skip:' "$TMP/suite.out"; then
  echo "BASELINE had an assertion that did not run:"
  grep '^unexpected skip:' "$TMP/suite.out"
  exit 1
fi
# Name the assertions that must have run. A suite whose sections were deleted
# also exits 0, and a baseline that only checks the exit code cannot tell the
# difference. This is the check case_ab86fd07 added to the capture suite.
for required in \
  "corroboratable (agent, run, company) triple" \
  "control call admitted" \
  "403 identity refusal attributable to the agent header alone" \
  "REFUSED (identity_argument_refused) and the queue is unchanged" \
  "the spoofed requester id appears nowhere in the queue" \
  "records requester=$LIVE_AGENT, the authenticated caller" \
  "403 run_not_live" \
  "401 without a bearer, and no WWW-Authenticate header"
do
  if ! grep -qF "PASS  $required" "$TMP/suite.out" && ! grep -qF "$required" "$TMP/suite.out"; then
    echo "BASELINE is green but never asserted: $required"
    RESULT=1
  fi
done
((RESULT == 0)) && echo "baseline green, all named assertions present"
((RESULT == 0)) || { cat "$TMP/suite.out"; exit 1; }
grep -c '^PASS' "$TMP/suite.out" | xargs printf 'baseline passes: %s\n'

# ===========================================================================
note "PHASE 2 — each control removed in turn must redden its OWN section"
# ===========================================================================
# The mutations model the real failure, not a syntax error. A mutation that
# merely breaks the server reddens everything and proves nothing about which
# assertion is load-bearing.
mutate() { # mutate <name> <expected-failing-section-substring> <sed-or-perl program>
  local name="$1" expect="$2" program="$3"
  local src="$TMP/mutant-$name.mjs"
  cp "$SERVER_SRC" "$src"
  perl -0pi -e "$program" "$src"
  if cmp -s "$SERVER_SRC" "$src"; then
    echo "MUTATION '$name' CHANGED NOTHING — the pattern no longer matches the source."
    echo "  This is the failure mode where a mutation gate silently stops testing."
    RESULT=1
    return
  fi
  if ! start_server "$src"; then
    echo "MUTATION '$name' made the server unstartable; that is a broken mutation, not a caught control."
    RESULT=1
    return
  fi
  local status; status="$(run_suite)"
  stop_server
  if [[ "$status" == "0" ]]; then
    echo "NOT CAUGHT: '$name' — the suite stayed green with that control removed."
    sed -n '/=== SUMMARY/,$p' "$TMP/suite.out"
    RESULT=1
    return
  fi
  if ! grep -q "^failed:.*$expect" "$TMP/suite.out"; then
    echo "WRONG SECTION: '$name' went red, but not in the section that owns it."
    echo "  expected a failure in: $expect"
    grep '^failed:' "$TMP/suite.out" || echo "  (no 'failed:' line — the suite aborted instead)"
    sed -n '/^FAIL/p;/^ABORT/p' "$TMP/suite.out" | head -5
    RESULT=1
    return
  fi
  printf 'caught: %-28s -> %s\n' "$name" "$(grep -m1 '^failed:' "$TMP/suite.out")"
}

# (a) A missing agent id silently resolves to a principal instead of refusing.
#     This is the hole the container suite found by mutation, and the reason
#     section 3 omits exactly one header and asserts on the MESSAGE: the code
#     `identity_header_missing` is still raised by the company and run arms, so
#     a code-only assertion survives this mutation.
mutate default-missing-agent-id "3." \
  "s/const agentId = headerValue\\(headers, \"x-paperclip-agent-id\"\\);/const agentId = headerValue(headers, \"x-paperclip-agent-id\") ?? \"00000000-0000-4000-8000-000000000002\";/"

# (b) The tool argument wins over the authenticated principal — the exact
#     failure the epic exists to prevent. Section 4 must see a row appear and
#     section 5 must see the wrong requester on it.
mutate requester-argument-wins "4." \
  "s/const argv = \\[\"submit\", \"--requester\", identity\\.agentId,/const argv = [\"submit\", \"--requester\", (typeof args.requester === \"string\" ? args.requester : identity.agentId),/; s/const bad = Object\\.keys\\(args\\)\\.find\\(\\(key\\) => FORBIDDEN_ARGUMENT_NAMES\\.has\\(key\\)\\);/const bad = undefined;/; s/assertKnownArguments\\(tool, args\\);/assertKnownArguments(tool, Object.fromEntries(Object.entries(args).filter(([k]) => k !== \"requester\")));/"

# (c) requireLiveRun stops corroborating — the header alone becomes proof.
mutate live-run-not-corroborated "6." \
  "s/    if \\(!cfg\\.requireLiveRun\\) return;/    if (true) return;/"

# (d) The bearer stops being checked.
mutate bearer-not-checked "7." \
  "s/    throw new HttpError\\(401, \"unauthorized\", \"invalid or missing bearer credential\"\\);/    return;/"

# ===========================================================================
note "PHASE 3 — the assertion this file replaces, shown to be vacuous"
# ===========================================================================
# Runbook step 9(1) sends a call with NO identity headers at all and asserts
# `403` / `identity_header_missing`. Run that exact assertion against the
# mutant from (a) — the one where a missing agent id resolves to a principal.
# If it still passes, the original step 9 could not have caught the very bug the
# container suite found by mutation, and section 3's one-input-at-a-time form is
# not stylistic.
MUTANT="$TMP/mutant-default-missing-agent-id.mjs"
if [[ ! -f "$MUTANT" ]]; then
  echo "phase 3 skipped: mutation (a) was never built"
  RESULT=1
else
  start_server "$MUTANT" || exit 1
  OLD_BODY="$TMP/old.json"
  OLD_CODE="$(curl -s -o "$OLD_BODY" -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $BEARER" -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"submit_provisioning_request","arguments":{"template":"E4_AUDIT_ANALYST","title":"t"}}}' \
    "http://127.0.0.1:$PORT/mcp")"
  OLD_ERRCODE="$(jq -r '.error.code // empty' "$OLD_BODY")"
  stop_server
  if [[ "$OLD_CODE" == "403" && "$OLD_ERRCODE" == "identity_header_missing" ]]; then
    echo "confirmed: old step 9(1) still reports 403/identity_header_missing against a server"
    echo "  that silently resolves a missing agent id to a principal. The assertion cannot"
    echo "  distinguish the two, because the company and run arms raise the same code."
  else
    echo "phase 3 did NOT reproduce: got $OLD_CODE/$OLD_ERRCODE."
    echo "  Either the mutation no longer models the bug, or the codes changed and"
    echo "  section 3's rationale needs rewriting rather than trusting."
    RESULT=1
  fi
fi

# ===========================================================================
note "PHASE 4 — an assertion that could not run must not read as a pass"
# ===========================================================================
# Section 5 has nothing to read when the queue legitimately refuses the submit,
# so it reports SKIP. A suite that then exits 0 would tell the operator the
# requester binding was verified when it never was — the single most expensive
# way for this file to be wrong.
start_server "$SERVER_SRC" || exit 1
SUITE_TEMPLATE=E9_REFUSED
STATUS4="$(run_suite)"
unset SUITE_TEMPLATE
stop_server
if [[ "$STATUS4" == "0" ]]; then
  echo "SKIP READ AS PASS: the suite exited 0 with section 5 unable to run."
  RESULT=1
elif ! grep -q '^unexpected skip: 5\.' "$TMP/suite.out"; then
  echo "the suite exited non-zero, but not because section 5 was skipped:"
  sed -n '/=== SUMMARY/,$p' "$TMP/suite.out"
  RESULT=1
else
  echo "confirmed: a skipped section 5 exits non-zero and names itself"
  grep -m1 '^RESULT:' "$TMP/suite.out"
fi

echo
if ((RESULT == 0)); then
  echo "SELF-TEST PASS — baseline green, all four controls load-bearing,"
  echo "and the assertion this replaces is demonstrably blind to the first of them."
else
  echo "SELF-TEST FAIL — see above."
fi
exit "$RESULT"
