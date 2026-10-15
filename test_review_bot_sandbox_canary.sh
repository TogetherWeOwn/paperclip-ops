#!/usr/bin/env bash
# ===========================================================================
# test_review_bot_sandbox_canary.sh -- hermetic tests for
# paperclip-upgrade/sandbox-canary.sh and its drain.sh verify gate (case_f5985bd1).
#
# No network, no docker, no real codex. A fake `codex` models the REAL
# codex-cli 0.159.2 command shape, which is the whole point of this suite:
# `codex sandbox [OPTIONS] [COMMAND]...` takes the command DIRECTLY and has no
# `linux` subcommand, so the first word is the program to run. The fake runs
# that program (exec "$@") whenever the "sandbox" would start, and prints the
# bwrap refusal and exits 1 when it would not. Measured on the real binary with
# the sandbox switched off: `codex sandbox echo x` exits 0 and prints x;
# `codex sandbox linux -- echo x` exits 1 (No such file or directory).
#
# Section 1 asserts the verdicts (exit codes, never message text).
# Section 2 runs the drain.sh gate extracted VERBATIM through a fake docker.
# Section 3 is the mutation self-check: each mutant of the canary must flip a
# verdict this suite asserts, so no assertion above is vacuous. The first
# mutant restores the card's original `codex sandbox linux --` form and
# proves it would have passed with the sandbox OPEN.
#
# Needs: bash, jq, coreutils (timeout, mktemp), python3 (mutants). Exit 0 all
# pass | 1 a check failed.
# ===========================================================================
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CANARY="$HERE/paperclip-upgrade/sandbox-canary.sh"
DRAIN="$HERE/paperclip-upgrade/drain.sh"
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "  ok   - $1"; }
no() { fail=$((fail + 1)); echo "  FAIL - $1"; }

for t in jq timeout mktemp python3; do
  command -v "$t" >/dev/null || { echo "REFUSED: $t is required" >&2; exit 2; }
done
[[ -f "$CANARY" && -f "$DRAIN" ]] || { echo "REFUSED: canary or drain.sh missing" >&2; exit 2; }

STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
BIN="$STAGE/bin"; mkdir -p "$BIN"

# --- fake codex: models codex-cli 0.159.2 -------------------------------------
cat > "$BIN/codex" <<'EOF'
#!/usr/bin/env bash
mode="${FAKE_CODEX_MODE:-closed}"
case "${1:-}" in
  --version) echo "codex-cli ${FAKE_CODEX_VERSION:-0.159.2}"; exit 0 ;;
  sandbox) shift ;;
  *) echo "unexpected: $*" >&2; exit 64 ;;
esac
sm=""
while (($#)); do
  case "$1" in
    -c) [[ "$2" == sandbox_mode=* ]] && sm="${2#sandbox_mode=}"; shift 2 ;;
    --) shift; break ;;
    -*) echo "error: unexpected argument '$1'" >&2; exit 2 ;;
    *) break ;;                       # NO `linux` subcommand: first word is the program
  esac
done
sm="${sm//\"/}"
if [[ "$sm" == danger-full-access ]]; then
  [[ "$mode" == broken-control ]] && { echo "error: unexpected argument" >&2; exit 2; }
  exec "$@"                           # sandbox off: the command simply runs
fi
case "$mode" in
  closed|broken-control) echo "bwrap: No permissions to create a new namespace" >&2; exit 1 ;;
  open)             exec "$@" ;;      # userns works: the shell starts
  open-no-net)      [[ "$1" == curl ]] && exit 7; exec "$@" ;;
  egress-only-open) [[ "$1" == echo ]] && { echo "bwrap: No permissions to create a new namespace" >&2; exit 1; }; exec "$@" ;;
  # non-zero but NOT the userns denial (CISO, case_f5985bd1): measured on 0.159.2,
  # TMPDIR=/proc/nonexistent makes the sandboxed step panic rc 101 with no bwrap text
  panic101)         echo "thread 'main' panicked: No space left on device" >&2; exit 101 ;;
  rc1-no-text)      echo "error: something else went wrong" >&2; exit 1 ;;
  rc2-with-text)    echo "bwrap: No permissions to create a new namespace" >&2; exit 2 ;;
  hang)             sleep 30; exit 1 ;;
  reads-stdin)      cat >/dev/null; echo "bwrap: No permissions to create a new namespace" >&2; exit 1 ;;
  *) echo "fake codex: unknown mode $mode" >&2; exit 64 ;;
esac
EOF
cat > "$BIN/curl" <<'EOF'
#!/usr/bin/env bash
want_code=0; for a in "$@"; do [[ "$a" == '%{http_code}' ]] && want_code=1; done
if [[ "${FAKE_CURL:-401}" == dead ]]; then ((want_code)) && printf 000; exit 7; fi
if ((want_code)); then printf '%s' "${FAKE_CURL:-401}"; else echo '{"error":"unauthorized"}'; fi
exit 0
EOF
chmod +x "$BIN/codex" "$BIN/curl"

# run SCRIPT MODE [ENV=VAL ...] -> OUT, RC
run() {
  local script="$1" mode="$2"; shift 2
  OUT="$(env PATH="$BIN:$PATH" FAKE_CODEX_MODE="$mode" "$@" bash "$script" 2>&1 </dev/null)"; RC=$?
}
want_rc() { # NAME WANT  (uses RC from the last run)
  [[ "$RC" == "$2" ]] && ok "$1 (rc=$RC)" || no "$1: rc=$RC, want $2 (got: $(head -c 200 <<<"$OUT" | tr '\n' '|'))"
}

# ============================================================================
echo "[1] verdicts"
# ============================================================================
run "$CANARY" closed;                          want_rc "sandbox closed, network up: PASS" 0
run "$CANARY" closed FAKE_CODEX_VERSION=0.160.0
want_rc "codex version change is recorded, not a failure" 0
grep -q 'step=version result=CHANGED' <<<"$OUT" && ok "version change is reported on the version line" || no "version change not reported"
run "$CANARY" open;                            want_rc "shell starts (userns works): BREACH" 3
run "$CANARY" open-no-net;                     want_rc "shell starts, codex blocks network: still BREACH" 3
run "$CANARY" egress-only-open;                want_rc "only sandboxed egress works: BREACH" 3
run "$CANARY" panic101;                        want_rc "sandboxed steps panic rc 101 with no bwrap text: INCONCLUSIVE, not a pass" 4
grep -q 'result=INCONCLUSIVE rc=101' <<<"$OUT" && ok "rc 101 is reported as INCONCLUSIVE on the step line" || no "rc 101 not reported as INCONCLUSIVE"
run "$CANARY" rc1-no-text;                     want_rc "exit 1 without the bwrap namespace text: INCONCLUSIVE, not a pass" 4
run "$CANARY" rc2-with-text;                   want_rc "bwrap text but exit 2: INCONCLUSIVE, not a pass" 4
run "$CANARY" closed FAKE_CURL=dead;           want_rc "closed but the network is dead: INCONCLUSIVE, not a pass" 4
run "$CANARY" broken-control;                  want_rc "control invocation cannot succeed: INCONCLUSIVE, not a pass" 4
S0=$SECONDS
run "$CANARY" hang SANDBOX_CANARY_TIMEOUT_SECS=1; want_rc "a hung sandbox step: INCONCLUSIVE, not a pass" 4
((SECONDS - S0 < 25)) && ok "hang is bounded by the step timeout" || no "hang took $((SECONDS - S0))s"
grep -q 'hung past' <<<"$OUT" && ok "a hung step is reported as hung, not as a bare non-zero exit" || no "a hung step is not reported as hung"

# The card's original form is vacuous on 0.159.2: with the sandbox OPEN it still
# exits non-zero, because `linux` is parsed as the program to run.
OLD_OUT="$(env PATH="$BIN:$PATH" FAKE_CODEX_MODE=open codex sandbox linux -- echo canary-ok 2>&1)"; OLD_RC=$?
[[ "$OLD_RC" != 0 ]] && ok "original 'codex sandbox linux -- echo' exits $OLD_RC with the sandbox OPEN (vacuous form)" \
  || no "original form exited 0 with the sandbox open; the vacuity premise is wrong"
NEW_OUT="$(env PATH="$BIN:$PATH" FAKE_CODEX_MODE=open codex sandbox -- echo canary-ok 2>&1)"; NEW_RC=$?
[[ "$NEW_RC" == 0 && "$NEW_OUT" == canary-ok ]] && ok "direct form 'codex sandbox -- echo' exits 0 with the sandbox open (it can see a breach)" \
  || no "direct form rc=$NEW_RC out=$NEW_OUT"

# piped exactly as drain.sh does it (docker exec -i ... bash -s): BASH_SOURCE is
# unset there, and a child that reads stdin would eat the rest of the script.
OUT="$(cat "$CANARY" | env PATH="$BIN:$PATH" FAKE_CODEX_MODE=closed bash -s 2>&1)"; RC=$?
want_rc "piped into bash -s: PASS (BASH_SOURCE unset)" 0
OUT="$(cat "$CANARY" | env PATH="$BIN:$PATH" FAKE_CODEX_MODE=reads-stdin bash -s 2>&1)"; RC=$?
want_rc "piped, a sandboxed child reads stdin: still PASS (stdin is /dev/null)" 0
grep -qx 'canary: PASS' <<<"$OUT" && ok "piped, stdin-reading child: the PASS line is printed (script not truncated)" || no "piped, stdin-reading child: no PASS line"

# refusals
run "$CANARY" closed SANDBOX_CANARY_URL='http://x -o/tmp/y';  want_rc "URL with whitespace is refused" 2
run "$CANARY" closed SANDBOX_CANARY_URL='file:///etc/passwd'; want_rc "non-http URL is refused" 2
run "$CANARY" closed SANDBOX_CANARY_TIMEOUT_SECS=0;           want_rc "zero timeout is refused" 2
OUT="$(env PATH="$BIN:$PATH" bash "$CANARY" --nope 2>&1 </dev/null)"; RC=$?;  want_rc "unknown argument is refused" 2
OUT="$(env PATH="$BIN:$PATH" bash "$CANARY" --help 2>&1 </dev/null)"; RC=$?;  want_rc "--help exits 0" 0
TOOLS="$STAGE/tools"; mkdir -p "$TOOLS"
for t in bash timeout mktemp head tr cut rm cat grep; do ln -s "$(command -v "$t")" "$TOOLS/$t"; done
ln -s "$BIN/curl" "$TOOLS/curl"                          # curl but NO codex
OUT="$(env -i PATH="$TOOLS" "$TOOLS/bash" "$CANARY" 2>&1 </dev/null)"; RC=$?
want_rc "codex missing is refused, never a pass (127 is not 'closed')" 2

# --- adapterConfig read-back -----------------------------------------------
adapter() { # NAME WANT JSON
  printf '%s' "$3" > "$STAGE/adapter.json"
  OUT="$(bash "$CANARY" --adapter-config "$STAGE/adapter.json" 2>&1 </dev/null)"; RC=$?
  want_rc "adapter: $1" "$2"
}
GOOD='{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":["-c","approval_policy=\"never\"","-s","read-only"]}'
adapter "bare adapterConfig, bypass false, -s read-only" 0 "$GOOD"
adapter "whole agent record with nested adapterConfig" 0 "{\"id\":\"x\",\"adapterConfig\":$GOOD}"
adapter "--sandbox spelling" 0 '{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":["--sandbox","read-only"]}'
adapter "sandbox_mode=read-only config override is fine" 0 '{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":["-s","read-only","-c","sandbox_mode=\"read-only\""]}'
adapter "bypass flag true (overrides -s)" 3 '{"dangerouslyBypassApprovalsAndSandbox":true,"extraArgs":["-s","read-only"]}'
adapter "bypass flag ABSENT (defaults true at creation)" 3 '{"extraArgs":["-s","read-only"]}'
adapter "bypass flag the string \"false\"" 3 '{"dangerouslyBypassApprovalsAndSandbox":"false","extraArgs":["-s","read-only"]}'
adapter "dangerouslyBypassSandbox alias true" 3 '{"dangerouslyBypassApprovalsAndSandbox":false,"dangerouslyBypassSandbox":true,"extraArgs":["-s","read-only"]}'
adapter "no -s in extraArgs" 3 '{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":["-c","approval_policy=\"never\""]}'
adapter "extraArgs absent" 3 '{"dangerouslyBypassApprovalsAndSandbox":false}'
adapter "a LATER -s workspace-write overrides read-only" 3 '{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":["-s","read-only","-s","workspace-write"]}'
adapter "-s danger-full-access" 3 '{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":["-s","danger-full-access"]}'
adapter "extraArgs carries --yolo" 3 '{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":["-s","read-only","--yolo"]}'
adapter "extraArgs carries the long bypass flag" 3 '{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":["--dangerously-bypass-approvals-and-sandbox","-s","read-only"]}'
adapter "sandbox_mode=danger-full-access config override" 3 '{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":["-s","read-only","-c","sandbox_mode=\"danger-full-access\""]}'
adapter "a LATER --sandbox=danger-full-access overrides -s read-only" 3 '{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":["-s","read-only","--sandbox=danger-full-access"]}'
adapter "a LATER attached -sworkspace-write overrides -s read-only" 3 '{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":["-s","read-only","-sworkspace-write"]}'
adapter "--sandbox=read-only alone is fine" 0 '{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":["--sandbox=read-only"]}'
adapter "attached -sread-only alone is fine" 0 '{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":["-sread-only"]}'
adapter "a later read-only restores it (the LAST flag wins)" 0 '{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":["--sandbox=danger-full-access","-s","read-only"]}'
adapter "a trailing -s with no value" 3 '{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":["-s","read-only","-s"]}'
adapter "extraArgs carries --full-auto (workspace-write)" 3 '{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":["-s","read-only","--full-auto"]}'
adapter "extraArgs is not an array" 3 '{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":"-s read-only"}'
adapter "malformed JSON is refused" 2 '{"dangerouslyBypass'
adapter "a JSON array is refused" 2 '[1,2]'
OUT="$(bash "$CANARY" --adapter-config "$STAGE/does-not-exist.json" 2>&1 </dev/null)"; RC=$?; want_rc "adapter: missing file is refused" 2
printf '%s' "$GOOD" > "$STAGE/real.json"; ln -sf "$STAGE/real.json" "$STAGE/link.json"
OUT="$(bash "$CANARY" --adapter-config "$STAGE/link.json" 2>&1 </dev/null)"; RC=$?; want_rc "adapter: a symlink is refused" 2

# ============================================================================
echo "[2] drain.sh verify gate (extracted verbatim, fake docker, REAL canary, fake codex)"
# ============================================================================
GATE="$STAGE/gate.sh"
sed -n '/^canary_breach_ack_ok() {/,/^}/p' "$DRAIN" > "$GATE"
sed -n '/^sandbox_canary_gate() {/,/^}/p' "$DRAIN" >> "$GATE"
[[ -s "$GATE" ]] || no "could not extract sandbox_canary_gate from drain.sh"
grep -q '^canary_breach_ack_ok() {' "$GATE" || no "could not extract canary_breach_ack_ok from drain.sh (the ack would be undefined in the gate)"
# Pin the Review Bot id the ack proofs: if it ever changes the ack fails closed.
DRAIN_BOT_ID="$(grep -E '^REVIEW_BOT_ID=' "$DRAIN" | head -n 1 | cut -d'"' -f2)"
[[ "$DRAIN_BOT_ID" == "00000000-0000-4000-8000-000000000008" ]] && ok "gate: Review Bot id pinned ($DRAIN_BOT_ID)" || no "gate: REVIEW_BOT_ID changed to '$DRAIN_BOT_ID' (the ack proof must follow it)"
cat > "$BIN/mockdocker" <<'EOF'
#!/usr/bin/env bash
# answers ONLY the exact exec drain.sh issues; anything else is a test failure
if [[ "$*" == "exec -i -u node mock-server bash -s" ]]; then
  [[ "${FAKE_DOCKER_FAIL:-}" == 1 ]] && { echo "Error: No such container: mock-server" >&2; exit 1; }
  [[ "${FAKE_DOCKER_SILENT:-}" == 1 ]] && exit 0       # exec ends 0 with no canary output (truncated run)
  exec bash -s
fi
echo "FATAL: mockdocker invoked outside the canary exec: $*" >&2; exit 99
EOF
chmod +x "$BIN/mockdocker"
gate_run() { # MODE [ENV=..]...  -> GRC, GOUT, GLOG
  local mode="$1"; shift
  GLOG="$STAGE/gate.log"; : > "$GLOG"
  GOUT="$( ( set -uo pipefail
      LOG="$GLOG"; _log_line() { printf '%s\n' "$*" >> "$LOG"; }; log() { printf '%s\n' "$*"; _log_line "$*"; }
      DOCKER="$BIN/mockdocker"; SERVER_CONTAINER=mock-server; CANARY="${GATE_CANARY:-$CANARY}"
      REVIEW_BOT_ID="00000000-0000-4000-8000-000000000008"; ACK_CANARY_BREACH=0
      psql_q() { cat >/dev/null; [[ "${FAKE_PSQL_FAIL:-0}" == 1 ]] && return 1; [[ "${FAKE_EMPTY_ROW:-0}" == 1 ]] && return 0; printf '%s|%s' "${FAKE_WOD:-<missing>}" "${FAKE_ENABLED:-<missing>}"; }
      export PATH="$BIN:$PATH" FAKE_CODEX_MODE="$mode"; for kv in "$@"; do export "${kv?}"; done
      source "$GATE"; sandbox_canary_gate ) 2>&1 )"; GRC=$?
}
gate_case() { # NAME WANT_RC MODE [ENV=..]...
  local name="$1" want="$2"; shift 2
  gate_run "$@"
  [[ "$GRC" == "$want" ]] && ok "gate: $name (rc=$GRC)" || no "gate: $name: rc=$GRC want $want (got: $(head -c 300 <<<"$GOUT" | tr '\n' '|'))"
}
gate_case "sandbox closed passes verify" 0 closed
gate_case "sandbox open FAILS verify (server stays under the hold)" 1 open
grep -q 'BREACH' "$GLOG" && ok "gate: breach is recorded in the drain log" || no "gate: breach not in the drain log"
gate_case "inconclusive (control broken) FAILS verify" 1 broken-control
gate_case "inconclusive (dead network) FAILS verify" 1 closed FAKE_CURL=dead
gate_case "inconclusive (panic rc 101, no bwrap text) FAILS verify" 1 panic101
gate_case "container transport failure FAILS verify" 1 closed FAKE_DOCKER_FAIL=1
gate_case "exit 0 WITHOUT the PASS line (truncated run) FAILS verify" 1 closed FAKE_DOCKER_SILENT=1
GATE_CANARY="$STAGE/missing.sh" gate_case "missing canary script FAILS verify" 1 closed
# --- --ack-canary-breach: dormancy needs BOTH heartbeat flags false -----------
# (Paperclip Review 4/5 on 0f86ee6: the ack read only wakeOnDemand and had no
# hermetic cases, so an always-ack mutant passed the whole suite.)
gate_case "breach without --ack-canary-breach FAILS verify even when the DB shows dormant" 1 open ACK_CANARY_BREACH=0 FAKE_WOD=false FAKE_ENABLED=false
gate_case "breach acked but wakeOnDemand=true FAILS verify" 1 open ACK_CANARY_BREACH=1 FAKE_WOD=true FAKE_ENABLED=false
gate_case "breach acked but wakeOnDemand missing FAILS verify" 1 open ACK_CANARY_BREACH=1 FAKE_WOD='<missing>' FAKE_ENABLED=false
gate_case "breach acked but the DB row is missing (empty read) FAILS verify" 1 open ACK_CANARY_BREACH=1 FAKE_EMPTY_ROW=1 FAKE_ENABLED=false
gate_case "breach acked but the DB is unreadable FAILS verify" 1 open ACK_CANARY_BREACH=1 FAKE_PSQL_FAIL=1 FAKE_WOD=false FAKE_ENABLED=false
gate_case "breach acked with wakeOnDemand=false but enabled=true FAILS verify (timer wakes)" 1 open ACK_CANARY_BREACH=1 FAKE_WOD=false FAKE_ENABLED=true
gate_case "breach acked with wakeOnDemand=false but enabled missing FAILS verify" 1 open ACK_CANARY_BREACH=1 FAKE_WOD=false FAKE_ENABLED='<missing>'
gate_case "breach acked with both flags false PASSES verify (logged recovery)" 0 open ACK_CANARY_BREACH=1 FAKE_WOD=false FAKE_ENABLED=false
gate_run open ACK_CANARY_BREACH=1 FAKE_WOD=false FAKE_ENABLED=false
grep -q 'ACKNOWLEDGED' "$GLOG" && ok "gate: ack is written to the drain log" || no "gate: ack not in the drain log"
gate_case "inconclusive acked with both flags false PASSES verify" 0 panic101 ACK_CANARY_BREACH=1 FAKE_WOD=false FAKE_ENABLED=false
gate_case "inconclusive acked with enabled=true FAILS verify" 1 panic101 ACK_CANARY_BREACH=1 FAKE_WOD=false FAKE_ENABLED=true
grep -Eq '^[[:space:]]*sandbox_canary_gate \|\| rc=1' "$DRAIN" \
  && ok "gate: wired into verify_held (an unwired gate would make section 2 vacuous)" \
  || no "gate: not wired into verify_held"
grep -q 'exec -i -u node "\$SERVER_CONTAINER" bash -s' "$DRAIN" \
  && ok "gate: exec shape matches the fake docker's exact-match answer" || no "gate: exec shape drifted from the fake docker"

# ============================================================================
echo "[3] mutation self-check: every mutant must flip a verdict asserted above"
# ============================================================================
# mutate NAME SCRIPT OLD NEW -> $STAGE/mut-NAME.sh ; fails if OLD is not found once
mutate() {
  python3 - "$2" "$STAGE/mut-$1.sh" "$3" "$4" <<'PY'
import sys
src, dst, old, new = sys.argv[1:5]
s = open(src).read()
if s.count(old) != 1:
    sys.exit(3)
open(dst, "w").write(s.replace(old, new))
PY
}
killed() { # NAME OLD NEW MODE WANT_ORIGINAL_RC [ENV=..]...   mutant must NOT return WANT
  local name="$1" old="$2" new="$3" mode="$4" want="$5"; shift 5
  if ! mutate "$name" "$CANARY" "$old" "$new"; then no "mutant $name: pattern not found exactly once (the canary changed; update the mutant)"; return; fi
  run "$STAGE/mut-$name.sh" "$mode" "$@"
  [[ "$RC" != "$want" ]] && ok "mutant $name killed (rc $RC, original $want)" || no "mutant $name SURVIVED: still rc=$RC in mode $mode"
}
# open-no-net: codex blocks the network, so the shell step is the ONLY detector
killed old-linux-form 'sandbox_mode="read-only"'"'"' -- echo canary-ok' 'sandbox_mode="read-only"'"'"' linux -- echo canary-ok' open-no-net 3
killed breach-never 'if [[ "$STEP_RC" == 0 ]]; then
    say "canary: step=$name result=BREACH' 'if [[ "$STEP_RC" == 98 ]]; then
    say "canary: step=$name result=BREACH' open 3
killed no-control '  INCONCLUSIVE=1
fi

# bwrap'"'"'s refusal' '  :
fi

# bwrap'"'"'s refusal' broken-control 4
# A hung step is INCONCLUSIVE on its own and through the non-bwrap branch, so the
# verdict cannot tell the mutant apart; the `hung` branch is pinned by its message.
if mutate hang-unrecognised "$CANARY" 'hung() { case "$1" in 124|137) return 0 ;; *) return 1 ;; esac; }' 'hung() { return 1; }'; then
  run "$STAGE/mut-hang-unrecognised.sh" hang SANDBOX_CANARY_TIMEOUT_SECS=1
  ! grep -q 'hung past' <<<"$OUT" && ok "mutant hang-unrecognised killed (the 'hung past' report is gone)" || no "mutant hang-unrecognised SURVIVED"
else no "mutant hang-unrecognised: pattern not found exactly once"; fi
killed dead-net-passes '(a dead network would make step 3 pass for free)"
  INCONCLUSIVE=1' '(a dead network would make step 3 pass for free)"
  :' closed 4 FAKE_CURL=dead
killed egress-skipped 'classify_closed egress "curl through the sandbox"' ':' egress-only-open 3
killed shell-skipped 'classify_closed shell "echo canary-ok"' ':' open-no-net 3
# CISO (case_f5985bd1): "closed" is exit 1 AND the bwrap namespace text. Each half has
# a mode that only it can catch; the any-non-zero mutant is the original defect.
killed closed-is-any-nonzero 'elif [[ "$STEP_RC" == 1 ]] && bwrap_namespace_refusal "$STEP_OUT"; then' 'elif true; then' panic101 4
killed closed-ignores-bwrap-text 'elif [[ "$STEP_RC" == 1 ]] && bwrap_namespace_refusal "$STEP_OUT"; then' 'elif [[ "$STEP_RC" == 1 ]]; then' rc1-no-text 4
killed closed-ignores-exit-code 'elif [[ "$STEP_RC" == 1 ]] && bwrap_namespace_refusal "$STEP_OUT"; then' 'elif bwrap_namespace_refusal "$STEP_OUT"; then' rc2-with-text 4
# adapter mutants need the adapter mode, so run them directly
amut() { # NAME OLD NEW JSON WANT
  if ! mutate "$1" "$CANARY" "$2" "$3"; then no "mutant $1: pattern not found exactly once"; return; fi
  printf '%s' "$4" > "$STAGE/amut.json"
  OUT="$(bash "$STAGE/mut-$1.sh" --adapter-config "$STAGE/amut.json" 2>&1 </dev/null)"; RC=$?
  [[ "$RC" != "$5" ]] && ok "mutant $1 killed (rc $RC, original $5)" || no "mutant $1 SURVIVED: still rc=$RC"
}
amut adapter-absent-passes '[[ "$bypass" == false ]] ||' '[[ "$bypass" != true ]] ||' '{"extraArgs":["-s","read-only"]}' 3
amut adapter-last-flag-ignored '[[ "$last_sandbox" == "read-only" ]] ||' ':  ||' '{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":["-s","danger-full-access"]}' 3
amut adapter-alias-ignored '[[ "$sandbox_alias" == absent || "$sandbox_alias" == boolean:false ]] ||' ':  ||' '{"dangerouslyBypassApprovalsAndSandbox":false,"dangerouslyBypassSandbox":true,"extraArgs":["-s","read-only"]}' 3
amut adapter-eq-form-ignored 'elif (\$x | startswith(\"--sandbox=\")) then (\$x | sub(\"^--sandbox=\"; \"\"))' 'elif false then \"\"' '{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":["-s","read-only","--sandbox=danger-full-access"]}' 3
amut adapter-attached-form-ignored 'elif (\$x | test(\"^-s.\")) then' 'elif false then' '{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":["-s","read-only","-sworkspace-write"]}' 3
amut adapter-full-auto-ignored 'or . == \"--full-auto\"' 'or . == \"--no-such-flag\"' '{"dangerouslyBypassApprovalsAndSandbox":false,"extraArgs":["-s","read-only","--full-auto"]}' 3
# stdin isolation needs the piped form
if mutate stdin "$CANARY" '</dev/null >"$STEP_OUT" 2>&1' '>"$STEP_OUT" 2>&1'; then
  OUT="$(cat "$STAGE/mut-stdin.sh" | env PATH="$BIN:$PATH" FAKE_CODEX_MODE=reads-stdin bash -s 2>&1)"; RC=$?
  # a script cut short by a child eating stdin ends rc 0 WITHOUT the PASS line
  { [[ "$RC" != 0 ]] || ! grep -qx 'canary: PASS' <<<"$OUT"; } && ok "mutant stdin killed (rc $RC, PASS line gone)" || no "mutant stdin SURVIVED"
else no "mutant stdin: pattern not found exactly once"; fi
# gate mutant: rc 0 without the PASS line is accepted
python3 - "$GATE" "$STAGE/gate-mut2.sh" <<'PY'
import sys
s = open(sys.argv[1]).read()
old = "grep -qx 'canary: PASS' <<<\"$out\""
if s.count(old) != 1: sys.exit(3)
open(sys.argv[2], "w").write(s.replace(old, ":"))
PY
if [[ -s "$STAGE/gate-mut2.sh" ]]; then
  cp "$GATE" "$STAGE/gate-orig2.sh"; cp "$STAGE/gate-mut2.sh" "$GATE"
  gate_run closed FAKE_DOCKER_SILENT=1
  [[ "$GRC" != 1 ]] && ok "mutant gate-accepts-truncated-run killed (rc $GRC, original 1)" || no "mutant gate-accepts-truncated-run SURVIVED"
  cp "$STAGE/gate-orig2.sh" "$GATE"
else no "mutant gate2: pattern not found exactly once"; fi
# gate mutant: a failing canary no longer fails verify
python3 - "$GATE" "$STAGE/gate-mut.sh" <<'PY'
import sys
s = open(sys.argv[1]).read()
old = '3) log "FAIL: review-bot sandbox canary BREACH'
if s.count(old) != 1: sys.exit(3)
open(sys.argv[2], "w").write(s.replace(old, '3) log "ok: ignored"; return 0 ;;\n    9) log "FAIL: review-bot sandbox canary BREACH', 1))
PY
if [[ -s "$STAGE/gate-mut.sh" ]]; then
  cp "$GATE" "$STAGE/gate-orig.sh"; cp "$STAGE/gate-mut.sh" "$GATE"
  gate_run open
  [[ "$GRC" != 1 ]] && ok "mutant gate-ignores-breach killed by the gate breach case (rc $GRC, original 1)" || no "mutant gate-ignores-breach SURVIVED"
  cp "$STAGE/gate-orig.sh" "$GATE"
else no "mutant gate: pattern not found exactly once"; fi
# gate mutant: the ack always succeeds with no flag and no DB proof
python3 - "$GATE" "$STAGE/gate-mut-ack.sh" <<'PY'
import sys
s = open(sys.argv[1]).read()
old = '((ACK_CANARY_BREACH == 1)) || return 1'
if s.count(old) != 1: sys.exit(3)
open(sys.argv[2], "w").write(s.replace(old, 'return 0'))
PY
if [[ -s "$STAGE/gate-mut-ack.sh" ]]; then
  cp "$GATE" "$STAGE/gate-orig-ack.sh"; cp "$STAGE/gate-mut-ack.sh" "$GATE"
  gate_run open
  [[ "$GRC" != 1 ]] && ok "mutant gate-always-ack killed (rc $GRC, original 1)" || no "mutant gate-always-ack SURVIVED"
  cp "$STAGE/gate-orig-ack.sh" "$GATE"
else no "mutant gate-always-ack: pattern not found exactly once"; fi
# gate mutant: the ack checks wakeOnDemand but ignores enabled (timer wakes)
python3 - "$GATE" "$STAGE/gate-mut-ack-enabled.sh" <<'PY'
import sys
s = open(sys.argv[1]).read()
old = '[[ "$wod" == false && "$enabled" == false ]]'
if s.count(old) != 1: sys.exit(3)
open(sys.argv[2], "w").write(s.replace(old, '[[ "$wod" == false ]]'))
PY
if [[ -s "$STAGE/gate-mut-ack-enabled.sh" ]]; then
  cp "$GATE" "$STAGE/gate-orig-ack-enabled.sh"; cp "$STAGE/gate-mut-ack-enabled.sh" "$GATE"
  gate_run open ACK_CANARY_BREACH=1 FAKE_WOD=false FAKE_ENABLED=true
  [[ "$GRC" != 1 ]] && ok "mutant gate-ack-ignores-enabled killed (rc $GRC, original 1)" || no "mutant gate-ack-ignores-enabled SURVIVED"
  cp "$STAGE/gate-orig-ack-enabled.sh" "$GATE"
else no "mutant gate-ack-ignores-enabled: pattern not found exactly once"; fi

echo
echo "  $pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
