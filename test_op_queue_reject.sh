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
#      rejected packet replays instead of re-verifying. Adversarial matrices
#      cover receipt I/O faults, processed-directory and audit-log trust, and
#      packet changes under the claim and during receipt construction.
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
command -v python3 >/dev/null || { echo "ERROR: python3 required" >&2; exit 1; }

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
if [[ "$(jq -r '.packet_sha256' <<<"$OUT")" == "sha256:$SHA_HAPPY" ]]; then
  ok "verify-only binds the payload hash"
else
  bad "verify-only hash mismatch" "$OUT"
fi

BEFORE_SHA="$(sha256sum -- "$Q/req-20261019T120000Z-000001.json" | cut -d' ' -f1)"
BEFORE_MTIME="$(stat -c %Y -- "$Q/req-20261019T120000Z-000001.json")"
run_tool reject --packet "$Q/req-20261019T120000Z-000001.json" --reason schema-probe
expect "reject writes a terminal receipt" 0 rejected ok
if jq -e '.outcome=="rejected" and .reason_code=="schema-probe"
       and .payload_preserved==true and .executed==false
       and .verb=="op-queue-reject" and .v==1' \
    -- "$P/req-20261019T120000Z-000001.json.rejected.json" >/dev/null; then
  ok "receipt records rejection + preserved + never-executed"
else
  bad "receipt missing or wrong" "$(cat "$P/req-20261019T120000Z-000001.json.rejected.json" 2>/dev/null)"
fi
if [[ "$(jq -r '.packet_sha256' -- "$P/req-20261019T120000Z-000001.json.rejected.json")" == "sha256:$SHA_HAPPY" ]]; then
  ok "receipt binds the exact payload bytes"
else
  bad "receipt hash does not bind the packet"
fi
if [ -f "$LOG" ] && grep -q 'req-20261019T120000Z-000001.json' "$LOG"; then
  ok "rejection log records the run"
else
  bad "rejection log missing the run"
fi
if [[ "$(sha256sum -- "$Q/req-20261019T120000Z-000001.json" | cut -d' ' -f1)" == "$BEFORE_SHA" ]]; then
  ok "packet bytes preserved through reject"
else
  bad "packet bytes CHANGED by reject"
fi
if [[ "$(stat -c %Y -- "$Q/req-20261019T120000Z-000001.json")" == "$BEFORE_MTIME" ]]; then
  ok "packet mtime untouched by reject"
else
  bad "packet mtime changed by reject"
fi

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
if [[ "$(sha256sum -- "$P/req-20261019T120000Z-000001.json.rejected.json" | cut -d' ' -f1)" == "$RECEIPT_SHA_BEFORE" ]]; then
  ok "replay leaves the receipt bytes untouched"
else
  bad "replay MODIFIED the receipt"
fi
if [[ "$(wc -l < "$LOG")" == "$LOG_LINES_BEFORE" ]]; then
  ok "replay appends no log line"
else
  bad "replay appended a log line"
fi

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
section "4b. delayed claims and receipt I/O faults"
# Keep the harness initialization intact; inject fixture shims only AFTER its
# BASH_ENV has run. Python bounds every wait and inspects actual bytes/JSON.
if python3 - "$TOOL" "$TMP" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

source = str(Path(sys.argv[1]).resolve())
root = Path(sys.argv[2]) / "faults"
queue, processed, bin_dir = (root / name for name in ("queue", "processed", "bin"))
for directory in (queue, processed, bin_dir):
    directory.mkdir(parents=True, exist_ok=True)
log = root / "audit.log"
env = os.environ | {
    "OP_REJECT_QUEUE_DIR": str(queue), "OP_REJECT_PROCESSED_DIR": str(processed),
    "OP_REJECT_LOG": str(log), "FIXTURE_BIN": str(bin_dir),
    "FIXTURE_SOURCE": source, "FIXTURE_ROOT": str(root),
    "REAL_MKDIR": shutil.which("mkdir"), "REAL_LN": shutil.which("ln"),
}
command = ["bash", "-c", 'export PATH="$FIXTURE_BIN:$PATH"; source "$FIXTURE_SOURCE" "$@"', "fixture"]

def packet(name):
    path = queue / (name + ".json")
    path.write_bytes(b'{"opaque":"$(touch never-execute)"}\n')
    path.chmod(0o644)
    return path

def receipt(path):
    return processed / (path.name + ".rejected.json")

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def run(path, reason="schema-probe", extra=None):
    result = subprocess.run(command + ["reject", "--packet", str(path), "--reason", reason],
                            env=env | (extra or {}), capture_output=True, text=True, timeout=10)
    return result.returncode, json.loads(result.stdout)

def shim(name, body):
    path = bin_dir / name
    path.write_text(body)
    path.chmod(0o755)
    return path

def log_lines():
    return log.read_text().splitlines() if log.exists() else []

def no_partial(path):
    assert not receipt(path).exists(), "invalid terminal receipt published"
    assert not (processed / (path.name + ".claim")).exists(), "failed attempt stranded its claim"

# B passes the early receipt test, then pauses just before acquiring its claim.
# A commits and releases the claim before B proceeds. No sleeps order the race.
mkdir_shim = shim("mkdir", '''#!/usr/bin/env python3
import os, pathlib, subprocess, sys, time
if os.environ.get("DELAY_CLAIM") == "1" and sys.argv[-1].endswith(".claim"):
    root = pathlib.Path(os.environ["FIXTURE_ROOT"])
    (root / "paused").touch()
    deadline = time.monotonic() + 8
    while not (root / "release").exists():
        if time.monotonic() >= deadline:
            sys.exit(90)
        time.sleep(0.01)
sys.exit(subprocess.run([os.environ["REAL_MKDIR"], *sys.argv[1:]]).returncode)
''')
path = packet("delayed")
packet_before = (sha(path), path.stat().st_mtime_ns)
delayed = subprocess.Popen(command + ["reject", "--packet", str(path), "--reason", "stale-packet"],
                           env=env | {"DELAY_CLAIM": "1"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
try:
    deadline = time.monotonic() + 8
    while not (root / "paused").exists():
        assert delayed.poll() is None and time.monotonic() < deadline, "claim barrier not reached"
        time.sleep(0.01)
    rc, result = run(path)
    assert rc == 0 and result["outcome"] == "rejected"
    original_receipt = sha(receipt(path))
    original_log = log.read_bytes()
    (root / "release").touch()
    stdout, stderr = delayed.communicate(timeout=10)
    assert delayed.returncode == 6 and json.loads(stdout)["reason_code"] == "replay_detected", (stdout, stderr)
    assert sha(receipt(path)) == original_receipt
    assert json.loads(receipt(path).read_text())["reason_code"] == "schema-probe"
    assert log.read_bytes() == original_log
    assert (sha(path), path.stat().st_mtime_ns) == packet_before
finally:
    if delayed.poll() is None:
        delayed.kill()
        delayed.communicate()
    mkdir_shim.unlink()
print("  delayed contender: one success, replay refusal, receipt/log/packet unchanged")

path = packet("jsonl")
assert run(path)[0] == 0
lines = log_lines()
assert len(lines) == 2 and all(json.loads(line)["outcome"] == "rejected" for line in lines)
print("  JSONL: exactly two successes, two independently valid physical lines")

# Failed scrub and corrupt-but-successful scrub must both fail closed.
for name, body in (("scrub-failure", "#!/bin/sh\nexit 1\n"),
                   ("invalid-scrub", "#!/bin/sh\nprintf 'not-json'\n"),
                   ("empty-scrub", "#!/bin/sh\nexit 0\n")):
    path = packet(name)
    before = sha(path), log.read_bytes()
    sed_shim = shim("sed", body)
    try:
        rc, result = run(path)
        assert rc == 1 and result["reason_code"] == "receipt_unwritten", (rc, result)
        no_partial(path)
        assert (sha(path), log.read_bytes()) == before
    finally:
        sed_shim.unlink()
    assert run(path)[0] == 0, "failed attempt prevents clean retry"
print("  scrub faults: nonzero, invalid JSON, and empty output publish nothing; clean retry succeeds")

# Bash printf is a builtin. Fault only its receipt-temp write, leaving machine
# stdout intact. Preserve the original BASH_ENV rather than replacing its policy.
fault_env = root / "write-fault-env.sh"
fault_env.write_text('''if [[ -n "${ORIGINAL_BASH_ENV:-}" ]]; then source "$ORIGINAL_BASH_ENV"; fi
printf() {
  if [[ "$(readlink /proc/$$/fd/1)" == */receipt.tmp ]]; then return 1; fi
  builtin printf "$@"
}
''')
path = packet("write-failure")
before_log = log.read_bytes()
rc, result = run(path, extra={"BASH_ENV": str(fault_env), "ORIGINAL_BASH_ENV": env.get("BASH_ENV", "")})
assert rc == 1 and result["reason_code"] == "receipt_unwritten", (rc, result)
no_partial(path)
assert log.read_bytes() == before_log
assert run(path)[0] == 0
print("  receipt write failure: no terminal record, no stranded claim, clean retry succeeds")

# Publication fails after the temp file exists; cleanup must remove only that
# owned temporary, never the packet or a committed terminal record.
path = packet("publish-failure")
ln_shim = shim("ln", "#!/bin/sh\nexit 1\n")
try:
    rc, result = run(path)
    assert rc == 1 and result["reason_code"] == "receipt_unwritten", (rc, result)
    no_partial(path)
finally:
    ln_shim.unlink()
assert run(path)[0] == 0
print("  publication failure: temporary and claim cleaned; clean retry succeeds")

# A receipt that appears at publication time is never clobbered, even if a
# writer outside the claim protocol creates it. The existing bytes win.
path = packet("publication-collision")
ln_shim = shim("ln", '''#!/usr/bin/env python3
import os, pathlib, subprocess, sys
pathlib.Path(sys.argv[-1]).write_bytes(b'{"existing":"terminal"}\\n')
sys.exit(subprocess.run([os.environ["REAL_LN"], *sys.argv[1:]]).returncode)
''')
before_log = log.read_bytes()
try:
    rc, result = run(path)
    assert rc == 6 and result["reason_code"] == "replay_detected", (rc, result)
    assert receipt(path).read_bytes() == b'{"existing":"terminal"}\n'
    assert log.read_bytes() == before_log
    assert not (processed / (path.name + ".claim")).exists()
finally:
    ln_shim.unlink()
print("  atomic publication collision: existing receipt preserved without second audit event")

# Inject an actual append failure on a trusted regular log, not an unsafe
# pathname that should be refused before receipt publication.
fault_env.write_text('''if [[ -n "${ORIGINAL_BASH_ENV:-}" ]]; then source "$ORIGINAL_BASH_ENV"; fi
printf() {
  if [[ "$(readlink /proc/$$/fd/1)" == "$OP_REJECT_LOG" ]]; then return 1; fi
  builtin printf "$@"
}
''')
path = packet("log-failure")
rc, result = run(path, extra={"BASH_ENV": str(fault_env), "ORIGINAL_BASH_ENV": env.get("BASH_ENV", "")})
assert rc == 1 and result["reason_code"] == "log_unwritten", (rc, result)
assert result["receipt"] == str(receipt(path))
assert json.loads(receipt(path).read_text())["outcome"] == "rejected"
terminal_sha = sha(receipt(path))
rc, result = run(path)
assert rc == 6 and result["reason_code"] == "replay_detected"
assert sha(receipt(path)) == terminal_sha
print("  log failure: I/O status reports committed receipt, which remains terminal on replay")
PY
then
  ok "adversarial receipt preservation, fault cleanup, JSONL, and log-failure contracts"
else
  bad "adversarial receipt regressions"
fi

# ===========================================================================
section "4c. processed-directory trust before replay and claim"
if python3 - "$TOOL" "$TMP" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

source = str(Path(sys.argv[1]).resolve())
root = Path(sys.argv[2]) / "directory-trust"
queue, bin_dir = root / "queue", root / "bin"
for directory in (queue, bin_dir):
    directory.mkdir(parents=True)
packet = queue / "directory-trust.json"
packet.write_bytes(b'{"opaque":"never execute"}\n')
packet.chmod(0o644)
log = root / "audit.log"
env = os.environ | {
    "OP_REJECT_QUEUE_DIR": str(queue), "OP_REJECT_LOG": str(log),
    "FIXTURE_BIN": str(bin_dir), "FIXTURE_SOURCE": source,
    "REAL_MKDIR": shutil.which("mkdir"), "REAL_STAT": shutil.which("stat"),
}
command = ["bash", "-c", 'umask 022; export PATH="$FIXTURE_BIN:$PATH"; source "$FIXTURE_SOURCE" "$@"', "fixture"]
before_packet = hashlib.sha256(packet.read_bytes()).hexdigest(), packet.stat().st_mtime_ns

def run(processed, mode="reject", extra=None):
    result = subprocess.run(command + [mode, "--packet", str(packet), "--reason", "schema-probe"],
                            env=env | {"OP_REJECT_PROCESSED_DIR": str(processed)} | (extra or {}),
                            capture_output=True, text=True, timeout=10)
    return result.returncode, json.loads(result.stdout)

def refused(processed, extra=None):
    for mode in ("verify-only", "reject"):
        rc, result = run(processed, mode, extra)
        assert (rc, result["outcome"], result["reason_code"]) == (5, "refused", "processed_dir_ownership"), (processed, mode, rc, result)
    assert not log.exists(), "unsafe directory wrote an audit event"
    assert (hashlib.sha256(packet.read_bytes()).hexdigest(), packet.stat().st_mtime_ns) == before_packet

for name, permissions in (("group-writable", 0o775), ("other-writable", 0o757)):
    processed = root / name
    processed.mkdir()
    processed.chmod(permissions)
    refused(processed)
    assert not list(processed.iterdir())
print("  writable processed directories refuse before verify-only or claim")

for state in ("receipt", "claim"):
    processed = root / ("forged-" + state)
    processed.mkdir()
    processed.chmod(0o777)
    forged = processed / (packet.name + (".rejected.json" if state == "receipt" else ".claim"))
    if state == "receipt":
        forged.write_bytes(b'{"forged":true}\n')
    else:
        forged.mkdir()
    refused(processed)
    assert list(processed.iterdir()) == [forged]
    assert forged.read_bytes() == b'{"forged":true}\n' if state == "receipt" else not list(forged.iterdir())
print("  forged receipt/claim in unsafe directory never counts as replay or busy state")

real_dir = root / "symlink-target"
real_dir.mkdir()
linked = root / "linked"
linked.symlink_to(real_dir, target_is_directory=True)
refused(linked)
refused(str(linked) + "///")
assert not list(real_dir.iterdir())
dangling = root / "dangling"
dangling.symlink_to(root / "missing-target", target_is_directory=True)
refused(dangling)
refused(str(dangling) + "///")
not_dir = root / "regular-file"
not_dir.write_bytes(b"preserve me\n")
refused(not_dir)
assert not_dir.read_bytes() == b"preserve me\n"
print("  symlink, dangling symlink and non-directory refuse without output mutation")

# Simulate only uid metadata: the packet is trusted, but the directory is not.
# This covers the production ownership branch without root or chown privileges.
processed = root / "nonroot-owner"
processed.mkdir()
stat_shim = bin_dir / "stat"
stat_shim.write_text('''#!/usr/bin/env python3
import os, subprocess, sys
if sys.argv[1:3] == ["-c", "%u"]:
    print("1000" if sys.argv[-1] == os.environ["OP_REJECT_PROCESSED_DIR"] else "0")
else:
    sys.exit(subprocess.run([os.environ["REAL_STAT"], *sys.argv[1:]]).returncode)
''')
stat_shim.chmod(0o755)
try:
    refused(processed, {"OP_REJECT_REQUIRE_ROOT_OWNERSHIP": "1"})
    assert not list(processed.iterdir())
finally:
    stat_shim.unlink()
print("  non-root processed owner refuses under production ownership rules (uid fixture)")

# The directory appears after the early existence check. Revalidate mkdir's
# result, including a substituted symlink, before any claim is attempted.
mkdir_shim = bin_dir / "mkdir"
mkdir_shim.write_text('''#!/usr/bin/env python3
import os, pathlib, subprocess, sys
if sys.argv[-1] == os.environ["OP_REJECT_PROCESSED_DIR"]:
    path = pathlib.Path(sys.argv[-1])
    if os.environ["CREATE_UNSAFE"] == "symlink":
        path.symlink_to(os.environ["FIXTURE_TARGET"], target_is_directory=True)
    else:
        path.mkdir()
        path.chmod(0o777)
    sys.exit(0)
sys.exit(subprocess.run([os.environ["REAL_MKDIR"], *sys.argv[1:]]).returncode)
''')
mkdir_shim.chmod(0o755)
try:
    for kind in ("writable", "symlink"):
        processed = root / ("created-" + kind)
        rc, result = run(processed, extra={"CREATE_UNSAFE": kind, "FIXTURE_TARGET": str(real_dir)})
        assert (rc, result["reason_code"]) == (5, "processed_dir_ownership"), (rc, result)
        assert not list(processed.iterdir()) and not log.exists()
finally:
    mkdir_shim.unlink()
print("  creation-time writable directory and symlink are rechecked before claim")

processed = root / "new-protected"
rc, result = run(processed, "verify-only")
assert (rc, result["reason_code"]) == (0, "would_reject")
assert not processed.exists() and not log.exists()
rc, result = run(processed)
assert (rc, result["outcome"]) == (0, "rejected")
assert not (processed.stat().st_mode & 0o022) and not processed.is_symlink()
assert (hashlib.sha256(packet.read_bytes()).hexdigest(), packet.stat().st_mtime_ns) == before_packet
print("  absent directory stays absent on verify-only; protected creation still succeeds")
PY
then
  ok "processed-directory trust, forged-state refusals, creation recheck and clean creation"
else
  bad "processed-directory trust regressions"
fi

# ===========================================================================
section "4d. audit-log trust before publication"
if python3 - "$TOOL" "$TMP" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

source = str(Path(sys.argv[1]).resolve())
root = Path(sys.argv[2]) / "log-trust"
queue, processed, bin_dir = (root / name for name in ("queue", "processed", "bin"))
for directory in (queue, processed, bin_dir):
    directory.mkdir(parents=True)
packet = queue / "log-trust.json"
packet.write_bytes(b'{"opaque":"never execute"}\n')
packet.chmod(0o644)
before = hashlib.sha256(packet.read_bytes()).hexdigest(), packet.stat().st_mtime_ns
receipt = processed / (packet.name + ".rejected.json")
claim = processed / (packet.name + ".claim")
env = os.environ | {
    "OP_REJECT_QUEUE_DIR": str(queue), "OP_REJECT_PROCESSED_DIR": str(processed),
    "FIXTURE_BIN": str(bin_dir), "FIXTURE_SOURCE": source,
    "REAL_STAT": shutil.which("stat"), "REAL_MKDIR": shutil.which("mkdir"),
}
command = ["bash", "-c", 'umask 077; export PATH="$FIXTURE_BIN:$PATH"; source "$FIXTURE_SOURCE" "$@"', "fixture"]

def run(log, mode="reject", extra=None):
    result = subprocess.run(command + [mode, "--packet", str(packet), "--reason", "schema-probe"],
                            env=env | {"OP_REJECT_LOG": str(log)} | (extra or {}),
                            capture_output=True, text=True, timeout=5)
    return result.returncode, json.loads(result.stdout)

def refused(log, extra=None):
    for mode in ("verify-only", "reject"):
        rc, result = run(log, mode, extra)
        assert (rc, result["outcome"], result["reason_code"]) == (5, "refused", "log_ownership"), (mode, log, rc, result)
        assert not receipt.exists() and not claim.exists(), "unsafe log published terminal state"
    assert (hashlib.sha256(packet.read_bytes()).hexdigest(), packet.stat().st_mtime_ns) == before

victim = root / "victim"
victim.write_bytes(b"preserve audit target\n")
linked = root / "linked.log"
linked.symlink_to(victim)
refused(linked)
assert victim.read_bytes() == b"preserve audit target\n"
dangling = root / "dangling.log"
dangling.symlink_to(root / "absent-target")
refused(dangling)
assert not (root / "absent-target").exists()
for name, mode in (("group", 0o664), ("other", 0o646), ("world", 0o666)):
    log = root / (name + ".log")
    log.write_bytes(b"preserve log\n")
    log.chmod(mode)
    refused(log)
    assert log.read_bytes() == b"preserve log\n"
fifo = root / "fifo.log"
os.mkfifo(fifo)
refused(fifo)
refused(root)
print("  symlink, dangling link, writable logs, FIFO and directory refuse with no publication or hang")

for name, kind in (("writable-parent", "writable"), ("linked-parent", "symlink"), ("missing-parent", "missing")):
    parent = root / name
    if kind == "writable":
        parent.mkdir()
        parent.chmod(0o777)
    elif kind == "symlink":
        parent.symlink_to(processed, target_is_directory=True)
    refused(parent / "new.log")
    assert not (parent / "new.log").exists()
print("  absent log requires a protected real parent, with no implicit directory creation")

# Simulate production uid checks for each trust object, without chown or root.
log = root / "nonroot.log"
log.write_bytes(b"preserve owner\n")
stat_shim = bin_dir / "stat"
stat_shim.write_text('''#!/usr/bin/env python3
import os, subprocess, sys
if sys.argv[1:3] == ["-c", "%u"]:
    print("1000" if sys.argv[-1] == os.environ["UNTRUSTED_UID_PATH"] else "0")
else:
    sys.exit(subprocess.run([os.environ["REAL_STAT"], *sys.argv[1:]]).returncode)
''')
stat_shim.chmod(0o755)
try:
    refused(log, {"OP_REJECT_REQUIRE_ROOT_OWNERSHIP": "1", "UNTRUSTED_UID_PATH": str(log)})
    refused(root / "absent.log", {"OP_REJECT_REQUIRE_ROOT_OWNERSHIP": "1", "UNTRUSTED_UID_PATH": str(root)})
finally:
    stat_shim.unlink()
assert log.read_bytes() == b"preserve owner\n"
print("  production non-root log and absent-log parent refuse (uid fixtures)")

# Unsafe log appears after preflight but before the claim is acquired.
late_log = root / "late.log"
mkdir_shim = bin_dir / "mkdir"
mkdir_shim.write_text('''#!/usr/bin/env python3
import os, pathlib, subprocess, sys
if sys.argv[-1].endswith(".claim"):
    pathlib.Path(os.environ["OP_REJECT_LOG"]).symlink_to(os.environ["FIXTURE_VICTIM"])
sys.exit(subprocess.run([os.environ["REAL_MKDIR"], *sys.argv[1:]]).returncode)
''')
mkdir_shim.chmod(0o755)
try:
    rc, result = run(late_log, extra={"FIXTURE_VICTIM": str(victim)})
    assert (rc, result["reason_code"]) == (5, "log_ownership"), (rc, result)
    assert not receipt.exists() and not claim.exists()
    assert victim.read_bytes() == b"preserve audit target\n"
finally:
    mkdir_shim.unlink()
print("  log revalidated under claim before terminal publication")

clean_log = root / "clean.log"
assert run(clean_log, "verify-only")[0] == 0
assert not clean_log.exists() and not receipt.exists()
assert run(clean_log)[0] == 0
assert len(clean_log.read_text().splitlines()) == 1
assert not (clean_log.stat().st_mode & 0o022)
print("  protected absent log is not created by verify-only; clean reject creates one audit event")
PY
then
  ok "audit-log type, mode, ownership, parent and late-substitution matrix"
else
  bad "audit-log trust regressions"
fi

# ===========================================================================
section "4e. packet binding rechecked under claim"
if python3 - "$TOOL" "$TMP" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

source = str(Path(sys.argv[1]).resolve())
root = Path(sys.argv[2]) / "packet-binding"
queue, processed, bin_dir = (root / name for name in ("queue", "processed", "bin"))
for directory in (queue, processed, bin_dir):
    directory.mkdir(parents=True)
log = root / "audit.log"
env = os.environ | {
    "OP_REJECT_QUEUE_DIR": str(queue), "OP_REJECT_PROCESSED_DIR": str(processed),
    "OP_REJECT_LOG": str(log), "FIXTURE_BIN": str(bin_dir), "FIXTURE_SOURCE": source,
    "REAL_MKDIR": shutil.which("mkdir"), "REAL_SED": shutil.which("sed"),
}
command = ["bash", "-c", 'export PATH="$FIXTURE_BIN:$PATH"; source "$FIXTURE_SOURCE" "$@"', "fixture"]
mutator = '''#!/usr/bin/env python3
import os, pathlib, subprocess, sys
when = os.environ["MUTATE_WHEN"]
if (when == "claim" and sys.argv[-1].endswith(".claim")) or when == "scrub":
    path = pathlib.Path(os.environ["FIXTURE_PACKET"])
    before = path.stat()
    kind = os.environ["MUTATE_KIND"]
    if kind == "same-size":
        path.write_bytes(b"B" * before.st_size)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif kind == "size":
        path.write_bytes(b"different size payload")
    elif kind == "mtime":
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1))
    elif kind == "symlink":
        target = path.with_suffix(".target")
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
    elif kind == "mode":
        path.chmod(0o666)
sys.exit(subprocess.run([os.environ["REAL_MKDIR" if when == "claim" else "REAL_SED"], *sys.argv[1:]]).returncode)
'''
for when in ("claim", "scrub"):
    for kind in ("same-size", "size", "mtime", "symlink", "mode"):
        packet = queue / (when + "-" + kind + ".json")
        packet.write_bytes(b"opaque initial bytes")
        packet.chmod(0o644)
        # Pin nanoseconds away from a second boundary: mtime-only must not hide
        # behind the second-resolution timestamp serialized into the receipt.
        os.utime(packet, ns=(1792436400000000000, 1792436400000000100))
        receipt = processed / (packet.name + ".rejected.json")
        claim = processed / (packet.name + ".claim")
        log_before = log.read_bytes() if log.exists() else b""
        shim = bin_dir / ("mkdir" if when == "claim" else "sed")
        shim.write_text(mutator)
        shim.chmod(0o755)
        try:
            result = subprocess.run(command + ["reject", "--packet", str(packet), "--reason", "schema-probe"],
                                    env=env | {"MUTATE_WHEN": when, "MUTATE_KIND": kind, "FIXTURE_PACKET": str(packet)},
                                    capture_output=True, text=True, timeout=10)
            output = json.loads(result.stdout)
            assert (result.returncode, output["reason_code"]) == (3, "packet_changed"), (when, kind, result.returncode, output)
            assert not receipt.exists() and not claim.exists(), "mutation left terminal or partial state"
            assert (log.read_bytes() if log.exists() else b"") == log_before
        finally:
            shim.unlink()
        # Repair only the hostile fixture shape; retry binds the current bytes.
        if packet.is_symlink():
            current = packet.read_bytes()
            packet.unlink()
            packet.write_bytes(current)
        packet.chmod(0o644)
        result = subprocess.run(command + ["reject", "--packet", str(packet), "--reason", "schema-probe"],
                                env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stdout
        record = json.loads(receipt.read_text())
        assert record["packet_sha256"] == "sha256:" + hashlib.sha256(packet.read_bytes()).hexdigest()
        assert record["packet_bytes"] == packet.stat().st_size
        print("  " + when + "/" + kind + ": no receipt/log/claim on change; clean retry binds current bytes")
PY
then
  ok "packet content, size, nanosecond mtime and trust mutations refuse before publication"
else
  bad "packet binding regressions"
fi

# ===========================================================================
section "4f. initial metadata is one validated snapshot"
if python3 - "$TOOL" "$TMP" <<'PY'
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

source = str(Path(sys.argv[1]).resolve())
root = Path(sys.argv[2]) / "initial-metadata"
queue, processed, bin_dir = (root / name for name in ("queue", "processed", "bin"))
for directory in (queue, processed, bin_dir):
    directory.mkdir(parents=True)
log = root / "audit.log"
trace = root / "stat-trace"
env = os.environ | {
    "OP_REJECT_QUEUE_DIR": str(queue), "OP_REJECT_PROCESSED_DIR": str(processed),
    "OP_REJECT_LOG": str(log), "FIXTURE_BIN": str(bin_dir), "FIXTURE_SOURCE": source,
    "REAL_STAT": shutil.which("stat"), "STAT_TRACE": str(trace),
}
command = ["bash", "-c", 'export PATH="$FIXTURE_BIN:$PATH"; source "$FIXTURE_SOURCE" "$@"', "fixture"]
shim = bin_dir / "stat"
shim.write_text('''#!/usr/bin/env python3
import os, pathlib, subprocess, sys
packet = pathlib.Path(os.environ["FIXTURE_PACKET"])
fmt = sys.argv[2] if sys.argv[1:2] == ["-c"] else ""
trace = pathlib.Path(os.environ["STAT_TRACE"])
kind = os.environ["SAMPLE_KIND"]
if sys.argv[-1] == str(packet):
    previous = trace.read_text().splitlines() if trace.exists() else []
    with trace.open("a") as out:
        out.write(fmt + "\\n")
    composite = fmt == "%d:%i:%s:%y"
    trigger = ((kind == "size" and fmt == "%s") or (kind == "mtime" and fmt == "%Y")
               or (kind.startswith("stamp-") and composite and fmt not in previous))
    if trigger:
        original, before = packet.read_bytes(), packet.stat()
        if kind.endswith("size"):
            packet.write_bytes(b"transient producer content with a different size\\n")
        else:
            os.utime(packet, ns=(before.st_atime_ns, before.st_mtime_ns + 5000000000))
        # Real filesystem mutation and real stat output, never fabricated metadata.
        result = subprocess.run([os.environ["REAL_STAT"], *sys.argv[1:]], capture_output=True)
        packet.write_bytes(original)
        os.utime(packet, ns=(before.st_atime_ns, before.st_mtime_ns))
        sys.stdout.buffer.write(result.stdout)
        sys.exit(result.returncode)
sys.exit(subprocess.run([os.environ["REAL_STAT"], *sys.argv[1:]]).returncode)
''')
shim.chmod(0o755)
failures = []
for kind in ("size", "mtime", "stamp-size", "stamp-mtime"):
    packet = queue / (kind + ".json")
    packet.write_bytes(b"original bytes\n")
    packet.chmod(0o644)
    os.utime(packet, ns=(1792436400000000100, 1792436400000000100))
    before = packet.read_bytes(), packet.stat()
    receipt = processed / (packet.name + ".rejected.json")
    claim = processed / (packet.name + ".claim")
    log_before = log.read_bytes() if log.exists() else b""
    trace.unlink(missing_ok=True)
    args = command + ["reject", "--packet", str(packet), "--reason", "stale-packet"]
    case_env = env | {"FIXTURE_PACKET": str(packet), "SAMPLE_KIND": kind}
    try:
        result = subprocess.run(args, env=case_env, capture_output=True, text=True, timeout=10)
        output = json.loads(result.stdout)
        formats = trace.read_text().splitlines()
        assert "%d:%i:%s:%y" in formats, "composite snapshot was not sampled"
        if kind.startswith("stamp-"):
            assert (result.returncode, output["reason_code"]) == (3, "packet_changed"), (kind, result.stdout)
            assert not receipt.exists() and not claim.exists()
            assert (log.read_bytes() if log.exists() else b"") == log_before
            # Stable retry must be accurate and must not inherit partial evidence.
            result = subprocess.run(args, env=case_env | {"PATH": os.environ["PATH"], "FIXTURE_BIN": str(root)},
                                    capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stdout
        record = json.loads(receipt.read_text())
        audit = json.loads(log.read_text().splitlines()[-1])
        assert record == audit and not claim.exists()
        terminal, audit_bytes = receipt.read_bytes(), log.read_bytes()
        retry = subprocess.run(args, env=case_env, capture_output=True, text=True, timeout=10)
        assert (retry.returncode, json.loads(retry.stdout)["reason_code"]) == (6, "replay_detected")
        assert receipt.read_bytes() == terminal and log.read_bytes() == audit_bytes
        assert packet.read_bytes() == before[0]
        assert (packet.stat().st_ino, packet.stat().st_mtime_ns) == (before[1].st_ino, before[1].st_mtime_ns)
        assert record["packet_sha256"] == "sha256:" + hashlib.sha256(before[0]).hexdigest()
        assert record["packet_bytes"] == before[1].st_size, (kind, record["packet_bytes"], before[1].st_size)
        expected_mtime = datetime.datetime.fromtimestamp(before[1].st_mtime_ns // 1000000000,
                                                        datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert record["packet_mtime"] == expected_mtime, (kind, record["packet_mtime"], expected_mtime)
        assert "%s" not in formats and "%Y" not in formats, "receipt metadata sampled separately"
        print("  " + kind + ": accurate receipt/audit snapshot, preserved packet and immutable replay")
    except AssertionError as error:
        failures.append((kind, str(error)))
assert not failures, failures
PY
then
  ok "changed-and-restored size/mtime cannot corrupt terminal snapshot; refusal retries cleanly"
else
  bad "initial metadata snapshot regressions"
fi

# ===========================================================================
section "4g. FIFO replacement at every hash is bounded"
if python3 - "$TOOL" "$TMP" <<'PY'
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

source = str(Path(sys.argv[1]).resolve())
root = Path(sys.argv[2]) / "bounded-hashes"
queue, processed, bin_dir = (root / name for name in ("queue", "processed", "bin"))
for directory in (queue, processed, bin_dir):
    directory.mkdir(parents=True)
log, counter, bounds = (root / name for name in ("audit.log", "count", "bounds"))
env = os.environ | {
    "OP_REJECT_QUEUE_DIR": str(queue), "OP_REJECT_PROCESSED_DIR": str(processed),
    "OP_REJECT_LOG": str(log), "FIXTURE_BIN": str(bin_dir), "FIXTURE_SOURCE": source,
    "REAL_SHA": shutil.which("sha256sum"), "REAL_TIMEOUT": shutil.which("timeout"),
    "HASH_COUNT": str(counter), "HASH_BOUNDS": str(bounds),
}
command = ["bash", "-c", 'export PATH="$FIXTURE_BIN:$PATH"; source "$FIXTURE_SOURCE" "$@"', "fixture"]
(bin_dir / "sha256sum").write_text('''#!/usr/bin/env python3
import os, pathlib, sys
counter = pathlib.Path(os.environ["HASH_COUNT"])
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
if count == int(os.environ["BLOCK_HASH"]):
    packet = pathlib.Path(os.environ["FIXTURE_PACKET"])
    packet.unlink()
    os.mkfifo(packet)
os.execv(os.environ["REAL_SHA"], [os.environ["REAL_SHA"], *sys.argv[1:]])
''')
(bin_dir / "timeout").write_text('''#!/usr/bin/env python3
import os, pathlib, sys
assert sys.argv[1:3] == ["30", "sha256sum"], sys.argv
with pathlib.Path(os.environ["HASH_BOUNDS"]).open("a") as out:
    out.write("30 sha256sum\\n")
# Exercise real timeout termination without adding 90 seconds to the offline tier.
os.execv(os.environ["REAL_TIMEOUT"], [os.environ["REAL_TIMEOUT"], "0.2", *sys.argv[2:]])
''')
for shim in bin_dir.iterdir():
    shim.chmod(0o755)
failures = []
for index in (1, 2, 3):
    packet = queue / ("hash-" + str(index) + ".json")
    packet.write_bytes(b"opaque bytes")
    packet.chmod(0o644)
    receipt = processed / (packet.name + ".rejected.json")
    claim = processed / (packet.name + ".claim")
    counter.unlink(missing_ok=True)
    bounds.unlink(missing_ok=True)
    log_before = log.read_bytes() if log.exists() else b""
    args = command + ["reject", "--packet", str(packet), "--reason", "schema-probe"]
    process = subprocess.Popen(args, env=env | {"BLOCK_HASH": str(index), "FIXTURE_PACKET": str(packet)},
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=5)
        result = json.loads(stdout)
        assert (process.returncode, result["reason_code"]) == (3, "packet_unreadable" if index == 1 else "packet_changed"), (index, stdout, stderr)
        assert int(counter.read_text()) == index and len(bounds.read_text().splitlines()) == index
        assert not receipt.exists() and not claim.exists()
        assert (log.read_bytes() if log.exists() else b"") == log_before
        # Only the test repairs its malicious FIFO; the helper has no packet write.
        packet.unlink()
        packet.write_bytes(b"opaque bytes")
        packet.chmod(0o644)
        retry = subprocess.run(args, env=env | {"FIXTURE_BIN": str(root)},
                               capture_output=True, text=True, timeout=10)
        assert retry.returncode == 0, retry.stdout
        assert json.loads(receipt.read_text())["packet_bytes"] == packet.stat().st_size
        print("  hash " + str(index) + ": FIFO read timed out, no terminal state, clean retry succeeds")
    except (AssertionError, subprocess.TimeoutExpired) as error:
        failures.append((index, str(error)))
    finally:
        # An unbounded older helper must not leave a blocked hash child behind.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
assert not failures, failures
PY
then
  ok "initial, under-claim and final packet hashes bound FIFO reads and clean owned state"
else
  bad "bounded packet hash regressions"
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
# Permit only the two fixed packet-read bounds, never a packet execution wrapper.
TIMEOUTS="$(grep -E '\btimeout\b' <<<"$CODE" || true)"
EXPECTED_TIMEOUTS="$(cat <<'EOF'
PACKET_SHA="$(timeout 30 sha256sum -- "$PACKET_REAL" | cut -d' ' -f1)" || PACKET_SHA=""
  sha="$(timeout 30 sha256sum -- "$PACKET_REAL" | cut -d' ' -f1)" || return 1
EOF
)"
if [[ "$TIMEOUTS" == "$EXPECTED_TIMEOUTS" ]]; then
  ok "timeouts limited to fixed packet hash reads"
else
  bad "unexpected or missing packet hash timeout" "$TIMEOUTS"
fi
structural "no permission mutation" '\b(chmod|chown|chgrp)\b'
# Only the acquired claim's fixed receipt.tmp may be removed. The payload and
# terminal receipt still have no deletion path; pin the sole exception exactly.
DELETIONS="$(grep -E '\b(rm|unlink|shred)\b' <<<"$CODE" || true)"
# These are literal source patterns, not shell expansions.
# shellcheck disable=SC2016
if [[ "$DELETIONS" == '  rm -f -- "$RECEIPT_TMP" 2>/dev/null || true' ]] \
   && grep -Fxq 'RECEIPT_TMP="$CLAIM/receipt.tmp"' <<<"$CODE"; then
  ok "deletion limited to owned claim temporary"
else
  bad "unexpected deletion path" "$DELETIONS"
fi
structural "no network" '\b(curl|wget|ssh|scp|sftp|nc|telnet)\b'
structural "no SHADOW coupling" 'SHADOW'
structural "no allowlist coupling" 'ALLOWLIST_|allowlist\.|Allowlist'
structural "no approval coupling" 'APPROVAL|approval|approver'
structural "no executor env reuse" 'OP_EXEC'
structural "no staging coupling" 'STAGING|staging'
structural "no sweep loop over the queue" 'for .* in .*QUEUE|find .*QUEUE|ls .*QUEUE'

# The queue dir gains no files on any path: reject, verify-only, and every
# refusal above must leave the drop dir exactly as the fixtures made it.
QUEUE_COUNT="$(find "$Q" -mindepth 1 -maxdepth 1 -printf '.\n' | wc -l)"
QUEUE_OUTPUTS="$(find "$Q" -mindepth 1 -maxdepth 1 \( -name '*.rejected.json' -o -name '*.claim' -o -name '*.tmp' \) -print)"
if [[ -n "$QUEUE_OUTPUTS" ]]; then
  bad "queue dir gained receipt/claim files" "$QUEUE_OUTPUTS"
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
