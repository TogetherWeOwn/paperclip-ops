#!/usr/bin/env bash
# ===========================================================================
# Offline regression suite for op_queue_reject.sh (receipt-only reject verb).
#
# NO NETWORK, NO CREDENTIALS, NO HOST MUTATION. Every case runs in a mktemp
# sandbox with fixed fixtures and a pinned clock (OP_REJECT_NOW), so
# receipts are deterministic. Assertions pin EXIT CODES and the
# machine-readable stdout JSON fields (via jq) — never stderr prose, which
# drifts by design. Fixture packet names are synthetic; no live queue file
# is ever touched.
#
# Section map (each is a required boundary for a non-executing verb):
#   1. happy paths: verify-only passes and writes nothing; reject writes one
#      receipt + one log line, binds the payload hash, preserves the packet
#      byte-for-byte, and accepts every enum reason.
#   2. evidence refusal: missing packet, unreadable packet, hostile
#      basename, unknown reason, malformed invocation.
#   3. reason boundary: the closed enum admits exactly three tokens.
#   4. replay/concurrency: second reject is a replay (receipt bytes and log
#      lines unchanged); pre-claimed packet is busy; verify-only on a
#      rejected packet replays instead of re-verifying.
#   5. filesystem boundary: packet outside the queue, symlink escape,
#      symlink-to-inside, group-writable packet, non-root packet under
#      production ownership rules.
#   6. non-execution structure: the verb carries no execution, approval,
#      allowlist, staging, or SHADOW path; the queue dir gains no files and
#      no packet byte changes on any path.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v jq >/dev/null || { echo "ERROR: jq required" >&2; exit 1; }
command -v timeout >/dev/null || { echo "ERROR: timeout required" >&2; exit 1; }

TOOL="${TOOL_UNDER_TEST:-$HERE/op_queue_reject.sh}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1"; [[ -n "${2:-}" ]] && printf '       %s\n' "$2"; }
section() { printf '\n== %s\n' "$1"; }

# ---- sandbox ---------------------------------------------------------------
Q="$TMP/queue"; P="$TMP/processed"
mkdir -p "$Q" "$P"
LOG="$TMP/rejection.log"
export OP_REJECT_QUEUE_DIR="$Q" OP_REJECT_PROCESSED_DIR="$P"
export OP_REJECT_LOG="$LOG" OP_REJECT_REQUIRE_ROOT_OWNERSHIP=0
export OP_REJECT_NOW="2026-10-19T19:00:00Z"

# ---- fixture builders ---------------------------------------------------------
# mk_packet NAME CONTENT: writes Q/NAME with mode 644, prints its sha256.
mk_packet() {
  local name="$1" content="$2"
  printf '%s' "$content" > "$Q/$name"
  chmod 644 "$Q/$name"
  sha256sum -- "$Q/$name" | cut -d' ' -f1
}

run_tool() { # args... -> sets RC, OUT (stdout json)
  OUT="$("$TOOL" "$@" 2>"$TMP/stderr.txt")"; RC=$?
}

expect() { # $1=label $2=want_rc $3=want_outcome $4=want_reason
  local label="$1" wrc="$2" wout="$3" wreason="$4" got_r got_o got_rr
  got_r="$RC"; got_o="$(jq -r '.outcome // "?"' <<<"$OUT" 2>/dev/null)"; got_rr="$(jq -r '.reason_code // "?"' <<<"$OUT" 2>/dev/null)"
  if [[ "$got_r" == "$wrc" && "$got_o" == "$wout" && "$got_rr" == "$wreason" ]]; then
    ok "$label (rc=$got_r $got_o/$got_rr)"
  else
    bad "$label (want rc=$wrc $wout/$wreason, got rc=$got_r $got_o/$got_rr)" "$OUT"
  fi
}

# ===========================================================================
section "1. happy paths"
SHA_HAPPY="$(mk_packet "req-20261019T120000Z-000001.json" '{"what":"schema probe","action":"true"}')"
OP_REJECT_QUEUE_DIR="$Q" run_tool verify-only --packet "$Q/req-20261019T120000Z-000001.json" --reason schema-probe
expect "verify-only passes on a clean packet" 0 verified would_reject
if [ -e "$P/req-20261019T120000Z-000001.json.rejected.json" ]; then
  bad "verify-only must not write a receipt"
else
  ok "verify-only writes no receipt"
fi
if [ -e "$LOG" ]; then
  bad "verify-only must not write a log line"
else
  ok "verify-only writes no log line"
fi
[[ "$(jq -r '.packet_sha256' <<<"$OUT")" == "sha256:$SHA_HAPPY" ]] \
  && ok "verify-only binds the payload hash" \
  || bad "verify-only hash mismatch" "$OUT"

BEFORE_SHA="$(sha256sum -- "$Q/req-20261019T120000Z-000001.json" | cut -d' ' -f1)"
BEFORE_MTIME="$(stat -c %Y -- "$Q/req-20261019T120000Z-000001.json")"
run_tool reject --packet "$Q/req-20261019T120000Z-000001.json" --reason schema-probe
expect "reject writes a terminal receipt" 0 rejected ok
jq -e '.outcome=="rejected" and .reason_code=="schema-probe"
       and .payload_preserved==true and .executed==false
       and .verb=="op-queue-reject" and .v==1' \
    -- "$P/req-20261019T120000Z-000001.json.rejected.json" >/dev/null \
  && ok "receipt records rejection + preserved + never-executed" \
  || bad "receipt missing or wrong" "$(cat "$P/req-20261019T120000Z-000001.json.rejected.json" 2>/dev/null)"
[[ "$(jq -r '.packet_sha256' -- "$P/req-20261019T120000Z-000001.json.rejected.json")" == "sha256:$SHA_HAPPY" ]] \
  && ok "receipt binds the exact payload bytes" \
  || bad "receipt hash does not bind the packet"
[ -f "$LOG" ] && grep -q 'req-20261019T120000Z-000001.json' "$LOG" \
  && ok "rejection log records the run" \
  || bad "rejection log missing the run"
[[ "$(sha256sum -- "$Q/req-20261019T120000Z-000001.json" | cut -d' ' -f1)" == "$BEFORE_SHA" ]] \
  && ok "packet bytes preserved through reject" \
  || bad "packet bytes CHANGED by reject"
[[ "$(stat -c %Y -- "$Q/req-20261019T120000Z-000001.json")" == "$BEFORE_MTIME" ]] \
  && ok "packet mtime untouched by reject" \
  || bad "packet mtime changed by reject"

section "1b. every enum reason is accepted"
for reason in schema-probe unknown-packet stale-packet; do
  n="req-reason-$reason.json"
  mk_packet "$n" '{"probe":true}' >/dev/null
  run_tool reject --packet "$Q/$n" --reason "$reason"
  expect "reason $reason accepted" 0 rejected ok
done

# ===========================================================================
section "2. evidence refusal"
run_tool reject --packet "$Q/no-such-packet.json" --reason schema-probe
expect "missing packet refuses as unavailable" 3 refused packet_unavailable

run_tool reject --packet "$Q/req-20261019T120000Z-000001.json"
# The usage path exits 2 with no stdout JSON; assert that shape exactly.
if [[ "$RC" == "2" && -z "$OUT" ]]; then
  ok "missing reason exits 2 with empty stdout (usage)"
else
  bad "missing reason shape" "rc=$RC out=$OUT"
fi
run_tool frobnicate --packet "$Q/req-20261019T120000Z-000001.json" --reason schema-probe
if [[ "$RC" == "2" && -z "$OUT" ]]; then
  ok "unknown mode exits 2 with empty stdout (usage)"
else
  bad "unknown mode shape" "rc=$RC out=$OUT"
fi

# Trailing valueless flags must exit 2, never hang: timeout fails closed
# (a regressed hang kills rc=124 and fails the case, not the suite).
timeout 10 "$TOOL" reject --packet >"$TMP/hang-out.txt" 2>"$TMP/stderr.txt"; RC=$?
if [[ "$RC" == "2" && ! -s "$TMP/hang-out.txt" ]]; then
  ok "trailing valueless --packet exits 2 (no hang)"
else
  bad "trailing valueless --packet" "rc=$RC"
fi
timeout 10 "$TOOL" reject --packet "$Q/req-20261019T120000Z-000001.json" --reason >"$TMP/hang-out.txt" 2>"$TMP/stderr.txt"; RC=$?
if [[ "$RC" == "2" && ! -s "$TMP/hang-out.txt" ]]; then
  ok "trailing valueless --reason exits 2 (no hang)"
else
  bad "trailing valueless --reason" "rc=$RC"
fi

# Hostile basenames: the CONTENT name is hostile while the queue path stays
# inside. Never let the hostile name near an ungated path.
trav() { # $1=hostile basename
  local hid="$1"
  printf '{"hostile":true}' > "$Q/$hid"
  chmod 644 "$Q/$hid"
  run_tool reject --packet "$Q/$hid" --reason schema-probe
  expect "hostile basename '$hid' refuses" 3 refused packet_name_rejected
  rm -f "$Q/$hid"
}
trav "-leading-dash.json"
trav "has space.json"
trav "semi;colon.json"
trav 'quote"tick.json'
trav "back\`tick.json"
LONG="$(printf 'a%.0s' {1..130}).json"
trav "$LONG"
leaked="$(ls -d "$P"/-leading-dash* "$P"/has* "$P"/semi* "$P"/quote* "$P"/back* 2>/dev/null || true)"
if [[ -n "$leaked" ]]; then
  bad "hostile basename wrote outside the jail" "$leaked"
else
  ok "hostile basenames wrote nothing outside the jail"
fi

# A directory where a packet should be is not a packet.
mkdir -p "$Q/not-a-packet.json"
run_tool reject --packet "$Q/not-a-packet.json" --reason schema-probe
expect "directory-as-packet refuses on ownership" 5 refused packet_ownership
rmdir "$Q/not-a-packet.json"

# Unreadable packet (owner-stripped mode bits; skipped as root, where mode
# bits do not bind and every fixture stays readable).
if [[ "$(id -u)" == "0" ]]; then
  ok "unreadable-packet case skipped as root (mode bits do not bind)"
else
  mk_packet "req-dark-000007.json" '{"probe":true}' >/dev/null
  chmod 000 "$Q/req-dark-000007.json"
  run_tool reject --packet "$Q/req-dark-000007.json" --reason schema-probe
  expect "unreadable packet refuses" 3 refused packet_unreadable
  chmod 644 "$Q/req-dark-000007.json"
  rm -f "$Q/req-dark-000007.json"
fi

# ===========================================================================
section "3. reason boundary"
t3() { # $1=label $2=reason $3=want_rc $4=want_outcome $5=want_reason
  local label="$1" reason="$2" wrc="$3" wout="$4" wr="$5" n="req-t3-$RANDOM.json"
  mk_packet "$n" '{"probe":true}' >/dev/null
  run_tool reject --packet "$Q/$n" --reason "$reason"
  expect "$label" "$wrc" "$wout" "$wr"
  rm -f "$Q/$n"
}
t3 "unknown reason refuses" "do-anything" 3 refused reason_not_allowlisted
t3 "shell metachars in reason refuse" 'schema-probe; touch x' 3 refused reason_not_allowlisted
t3 "path as reason refuses" '/bin/sh' 3 refused reason_not_allowlisted
t3 "empty-adjacent reason refuses" 'Schema-Probe' 3 refused reason_not_allowlisted
t3 "reason with space refuses" 'schema probe' 3 refused reason_not_allowlisted

# ===========================================================================
section "4. replay, concurrency, no-retry"
RECEIPT_SHA_BEFORE="$(sha256sum -- "$P/req-20261019T120000Z-000001.json.rejected.json" | cut -d' ' -f1)"
LOG_LINES_BEFORE="$(wc -l < "$LOG")"
run_tool reject --packet "$Q/req-20261019T120000Z-000001.json" --reason stale-packet
expect "second reject is a replay (even with a new reason)" 6 refused replay_detected
[[ "$(sha256sum -- "$P/req-20261019T120000Z-000001.json.rejected.json" | cut -d' ' -f1)" == "$RECEIPT_SHA_BEFORE" ]] \
  && ok "replay leaves the receipt bytes untouched" \
  || bad "replay MODIFIED the receipt"
[[ "$(wc -l < "$LOG")" == "$LOG_LINES_BEFORE" ]] \
  && ok "replay appends no log line" \
  || bad "replay appended a log line"

mk_packet "req-busy-000002.json" '{"probe":true}' >/dev/null
mkdir -p "$P/req-busy-000002.json.claim"
run_tool reject --packet "$Q/req-busy-000002.json" --reason schema-probe
expect "pre-claimed packet is busy" 6 refused concurrent_claim
rmdir "$P/req-busy-000002.json.claim"

run_tool verify-only --packet "$Q/req-20261019T120000Z-000001.json" --reason schema-probe
expect "verify-only on a rejected packet replays" 6 refused replay_detected

# An unwritable processed dir is I/O failure, not a concurrent claim.
mk_packet "req-noproc-000006.json" '{"probe":true}' >/dev/null
OP_REJECT_PROCESSED_DIR="$TMP/no-such-parent/sub" run_tool reject --packet "$Q/req-noproc-000006.json" --reason schema-probe
expect "unwritable processed dir fails as I/O, not a claim" 1 failed processed_dir_unwritable
if [ -e "$TMP/no-such-parent" ]; then
  bad "I/O failure must not create parents"
else
  ok "I/O failure creates nothing"
fi

# ===========================================================================
section "5. filesystem boundary"
OUTSIDE="$TMP/outside.json"; printf '{"outside":true}' > "$OUTSIDE"
run_tool reject --packet "$OUTSIDE" --reason schema-probe
expect "packet outside queue refuses" 5 refused packet_outside_queue

ln -sf "$OUTSIDE" "$Q/link.json"
run_tool reject --packet "$Q/link.json" --reason schema-probe
expect "symlink escape refuses at the symlink gate" 5 refused packet_is_symlink
rm -f "$Q/link.json"

# Symlink to a file that IS inside still refuses: the receipt name must
# equal the invoked name, and a link would force the verb to guess.
mk_packet "req-real-000003.json" '{"probe":true}' >/dev/null
ln -sf "$Q/req-real-000003.json" "$Q/alias.json"
run_tool reject --packet "$Q/alias.json" --reason schema-probe
expect "symlink-to-inside refuses" 5 refused packet_is_symlink
rm -f "$Q/alias.json"

# Group-writable packet refuses even with ownership checks relaxed.
mk_packet "req-loose-000004.json" '{"probe":true}' >/dev/null
chmod 664 "$Q/req-loose-000004.json"
run_tool reject --packet "$Q/req-loose-000004.json" --reason schema-probe
expect "group-writable packet refuses" 5 refused packet_ownership

# Non-root-owned packet refuses under production ownership rules.
# The fixture packets are user-owned, so this must refuse on ownership even
# though every other check would pass. Skipped as root, where every
# fixture is root-owned and the gate correctly passes instead.
if [[ "$(id -u)" == "0" ]]; then
  ok "user-owned packet case skipped as root (fixtures are root-owned)"
else
  mk_packet "req-own-000005.json" '{"probe":true}' >/dev/null
  OP_REJECT_REQUIRE_ROOT_OWNERSHIP=1 run_tool reject --packet "$Q/req-own-000005.json" --reason schema-probe
  expect "user-owned packet refuses when root ownership required" 5 refused packet_ownership
fi

# ===========================================================================
section "6. non-execution structure"
CODE="$(grep -v '^[[:space:]]*#' -- "$TOOL")"
structural() { # $1=label $2=egrep-pattern (must match NOTHING in code lines)
  local label="$1" pat="$2" hits
  hits="$(grep -nE "$pat" <<<"$CODE" || true)"
  if [[ -z "$hits" ]]; then
    ok "$label"
  else
    bad "$label" "$hits"
  fi
}
structural "no eval" '\beval\b'
structural "no shell passthrough" 'bash +-c'
structural "no backticks" '`'
structural "no exec builtin" '(^|[;&|])[[:space:]]*exec[[:space:]]'
structural "no sourced scripts" '(^|[;&|])[[:space:]]*(source[[:space:]]|\.[[:space:]]+/)'
structural "no timeout wrapper (nothing runs)" '\btimeout\b'
structural "no permission mutation" '\b(chmod|chown|chgrp)\b'
structural "no deletion" '\b(rm|unlink|shred)\b'
structural "no network" '\b(curl|wget|ssh|scp|sftp|nc|telnet)\b'
structural "no SHADOW coupling" 'SHADOW'
structural "no allowlist coupling" 'ALLOWLIST_|allowlist\.|Allowlist'
structural "no approval coupling" 'APPROVAL|approval|approver'
structural "no executor env reuse" 'OP_EXEC'
structural "no staging coupling" 'STAGING|staging'
structural "no sweep loop over the queue" 'for .* in .*QUEUE|find .*QUEUE|ls .*QUEUE'

# The queue dir gains no files on any path: reject, verify-only, and every
# refusal above must leave the drop dir exactly as the fixtures made it.
QUEUE_COUNT="$(ls -1 "$Q" | wc -l)"
if ls "$Q" | grep -qE '\.rejected\.json$|\.claim$|\.tmp$'; then
  bad "queue dir gained receipt/claim files" "$(ls -1 "$Q")"
else
  ok "queue dir holds only packet fixtures ($QUEUE_COUNT files, no receipts)"
fi

# Every packet still on disk is byte-identical to its fixture write: the
# verb reads packets and writes only under the processed dir + log.
if grep -qE '>|>>' <<<"$(grep -nE 'QUEUE_DIR' <<<"$CODE" | grep -v 'within_dir_strict\|QUEUE_DIR:-' || true)"; then
  bad "code writes through QUEUE_DIR" "$(grep -nE 'QUEUE_DIR' <<<"$CODE")"
else
  ok "no write path references the queue dir"
fi

# ===========================================================================
printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" == "0" ]]
