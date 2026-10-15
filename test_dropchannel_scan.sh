#!/usr/bin/env bash
# Regression suite for dropchannel_scan.sh — case_5276d981.
#
# WHAT THIS PROTECTS. dropchannel_scan.sh is the operator-run redaction scanner
# for /paperclip/operator-handoff: the independent check that the handoff
# allowlist held on a path world-readable to every agent on the box. The tool
# already carries an extensive built-in `selftest`, and CI runs that selftest.
# This suite is the OUTSIDE view of the same tool: it drives the shipped `scan`
# subcommand against throwaway fixture directories the way an operator or cron
# would, and pins the four properties the card names:
#
#   1. A clean handoff dir reports CLEAN (exit 0) — and says how many files it
#      read, so a CLEAN that looked nowhere cannot pass.
#   2. A planted secret assignment is a FAIL (exit 1) reported as `file:line`,
#      and the report never echoes the secret itself.
#   3. Mode and ownership violations fail: a world-writable root without the
#      sticky bit, and an agent-owned directory on a shared-uid host.
#   4. The scan is read-only outside its own temp dir: the fixture tree is
#      byte-identical before and after every read subcommand.
#
# THE TRAP IN TESTING (same shape as the git-remote-credential suite). A scan
# that matched nothing at all would pass "clean stays clean" perfectly, so
# every negative assertion here is PAIRED with a positive one on the same run,
# and section 5 dulls one detection pattern in a staged copy and shows the
# planted fixture going uncaught — proving the positive cases are load-bearing
# rather than passing for an unrelated reason.
#
# Offline and credential-free by construction: every fixture is a mktemp dir
# with fabricated strings; the shared-uid detector is driven by fixture passwd
# files, never the runner's own /etc/passwd; no network, no real secret.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="${DROPCHANNEL_SCAN_SH:-$HERE/dropchannel_scan.sh}"
PASS=0; FAIL=0

ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); [[ -n "${2:-}" ]] && printf '        %s\n' "$2"; }
hdr() { printf '\n\033[1m%s\033[0m\n' "$1"; }

[[ -x "$TOOL" ]] || { echo "ERROR: $TOOL is not executable" >&2; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test_dropchannel.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# ASSEMBLED AT RUNTIME, NEVER WRITTEN OUT WHOLE. Two readers punish a literal
# here: the CI Secret scan fails the job on a token-shaped string, and section
# 6 scans a copy of THIS file with the tool itself — a literal credential line
# would turn that self-scan red for a reason unrelated to the tool. The
# scanner's own source keeps the same discipline for the same reason.
KEY_HEAD="management_"
KEY_TAIL="pass""word"
PLANTED_KEY="${KEY_HEAD}${KEY_TAIL}"
PLANTED_VAL="hunter2-correct-horse"

hdr "1. A clean handoff dir reports CLEAN"
CLEAN="$WORK/clean"
mkdir -p "$CLEAN"; chmod 0755 "$CLEAN"
cat > "$CLEAN/handoff.md" <<'EOF'
# Routine handoff note
Run the drop-guard step before the swap, then record the row counts.
Log file mode: 0600   tokens_input = 41234   api_key: never write one here
combo id 00000000-0000-4000-8000-000000000006
backup verified sha256=084ad4b5c2119f0e7a3d5b6c8e9f0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b
The management password is loaded from a 0600 file into the environment, never argv.
EOF
OUT="$(DROPSCAN_SHARED_UID=0 "$TOOL" scan --root "$CLEAN" 2>&1)"; RC=$?
[[ $RC -eq 0 ]] && ok 'exit 0 on a clean handoff dir' || bad "expected exit 0, got $RC" "$OUT"
grep -qF 'CLEAN — every file read in full' <<<"$OUT" \
  && ok 'prints a CLEAN banner' || bad 'no CLEAN banner' "$OUT"
grep -qE 'files scanned : [1-9]' <<<"$OUT" \
  && ok 'reports a non-zero file count (a CLEAN that looked nowhere is not clean)' \
  || bad 'did not report how many files it scanned' "$OUT"

hdr "2. A planted secret assignment fails with file:line"
LEAK="$WORK/leak"
mkdir -p "$LEAK"; chmod 0755 "$LEAK"
cp "$CLEAN/handoff.md" "$LEAK/handoff.md"
{
  echo '# operator note'
  echo 'channel: routine'
  printf '%s = %s\n' "$PLANTED_KEY" "$PLANTED_VAL"
} > "$LEAK/planted.txt"
OUT="$(DROPSCAN_SHARED_UID=0 "$TOOL" scan --root "$LEAK" 2>&1)"; RC=$?
[[ $RC -eq 1 ]] && ok 'exit 1 on a planted secret' || bad "expected exit 1, got $RC" "$OUT"
grep -qF "FAIL  planted.txt  line 3" <<<"$OUT" \
  && ok 'finding names the file and the line' || bad 'no file:line finding' "$OUT"
grep -q 'rule=secret-assignment' <<<"$OUT" \
  && ok 'the firing rule is secret-assignment' || bad 'wrong or missing rule' "$OUT"
grep -qF "$PLANTED_VAL" <<<"$OUT" \
  && bad 'THE SCANNER PRINTED THE SECRET' || ok 'planted value absent from output'
grep -qF "fp=$(printf '%s' "$PLANTED_VAL" | sha256sum | cut -c1-12)" <<<"$OUT" \
  && ok 'fingerprint reported instead of the value' || bad 'no fingerprint in output' "$OUT"
# Pairing guard: the benign file beside the planted one stayed quiet, so the
# negatives in section 1 are not vacuous and this positive is not a blanket red.
# (The `FAIL findings : N` summary line starts with FAIL too, so the count
# anchors on the two-space finding format `FAIL  path  loc`.)
n="$(grep -c '^FAIL  ' <<<"$OUT")"
[[ "$n" -eq 1 ]] && ok 'exactly 1 FAIL — the benign file beside it stayed quiet' \
  || bad "expected exactly 1 FAIL, got $n" "$OUT"

hdr "3. Mode and ownership violations fail"
WW="$WORK/worldwrite"
mkdir -p "$WW"; echo "hello" > "$WW/ok.txt"; chmod 0777 "$WW"
OUT="$(DROPSCAN_SHARED_UID=0 "$TOOL" scan --root "$WW" 2>&1)"; RC=$?
[[ $RC -eq 1 ]] && ok 'world-writable root without sticky exits 1' \
  || bad "expected exit 1, got $RC" "$OUT"
grep -q 'rule=channel-writable-no-sticky' <<<"$OUT" \
  && ok 'the firing rule is channel-writable-no-sticky' || bad 'wrong or missing rule' "$OUT"
chmod 0755 "$WW"
OUT="$(DROPSCAN_SHARED_UID=0 "$TOOL" scan --root "$WW" 2>&1)"; RC=$?
[[ $RC -eq 0 ]] && ok 'same dir at 0755 is clean (control)' \
  || bad "expected control exit 0, got $RC" "$OUT"

# Ownership half: the verdicts are driven by FIXTURE passwd files, never the
# runner's own /etc/passwd, so these assert the same thing on a shared-uid box
# and on an ordinary multi-user one.
PW1="$WORK/passwd.single"; PW2="$WORK/passwd.multi"
{
  echo "root:x:0:0:root:/root:/bin/bash"
  echo "daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin"
  echo "svc:x:998:998::/nonexistent:/usr/sbin/nologin"
  echo "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin"
  echo "node:x:1000:1000::/paperclip:/bin/bash"
} > "$PW1"
cp "$PW1" "$PW2"
echo "alice:x:1001:1001::/home/alice:/bin/bash" >> "$PW2"
OWN="$WORK/owned"
mkdir -p "$OWN"; chmod 0755 "$OWN"; echo "routine handoff note" > "$OWN/probe.md"
OUT="$(DROPSCAN_SHARED_UID=auto DROPSCAN_PASSWD_FILE="$PW1" "$TOOL" scan --root "$OWN" 2>&1)"; RC=$?
[[ $RC -eq 1 ]] && ok 'agent-owned 0755 dir fails on a shared-uid host' \
  || bad "expected exit 1, got $RC" "$OUT"
grep -q 'rule=dir-agent-owned-shared-uid' <<<"$OUT" \
  && ok 'the firing rule is dir-agent-owned-shared-uid' || bad 'wrong or missing rule' "$OUT"
OUT="$(DROPSCAN_SHARED_UID=auto DROPSCAN_PASSWD_FILE="$PW2" "$TOOL" scan --root "$OWN" 2>&1)"; RC=$?
[[ $RC -eq 0 ]] && ok 'same dir is clean where the uid is not shared (control)' \
  || bad "expected control exit 0, got $RC" "$OUT"

hdr "4. The scan writes nothing outside its own temp dir"
snap() {
  (cd "$LEAK" && find . | sort; find . -type f -exec sha256sum {} + | sort \
   ; find . -exec stat -c '%a %u %g %n' {} + | sort)
}
BEFORE="$(snap)"
DROPSCAN_SHARED_UID=0 "$TOOL" scan --root "$LEAK" >/dev/null 2>&1 || true
DROPSCAN_SHARED_UID=0 "$TOOL" manifest --root "$LEAK" > "$WORK/ro.manifest" 2>/dev/null
VOUT="$(DROPSCAN_SHARED_UID=0 "$TOOL" verify --manifest "$WORK/ro.manifest" --root "$LEAK" 2>&1)"; VRC=$?
[[ $VRC -eq 0 ]] && ok 'verify agrees the channel is unchanged' \
  || bad "expected verify exit 0, got $VRC" "$VOUT"
AFTER="$(snap)"
if [[ "$BEFORE" == "$AFTER" ]]; then
  ok 'fixture tree byte-identical across scan, manifest and verify'
else
  bad 'fixture tree changed under a read-only scan'
fi

hdr "5. MUTATION: dulling one detection pattern must cost the planted catch"
STAGE="$WORK/mutant"
mkdir -p "$STAGE"; cp "$TOOL" "$STAGE/scan.sh"; chmod +x "$STAGE/scan.sh"
MUT_OK=1
OLD='("secret-assignment", "FAIL", CRED_ASSIGN_RE,' \
NEW='("secret-assignment", "FAIL", re.compile(r"(?!)"),' \
F="$STAGE/scan.sh" python3 - <<'PY' || MUT_OK=0
import os
f = os.environ["F"]; old = os.environ["OLD"]; new = os.environ["NEW"]
s = open(f).read()
assert s.count(old) == 1, f"anchor appears {s.count(old)} times, expected 1"
open(f, "w").write(s.replace(old, new))
PY
[[ $MUT_OK -eq 1 ]] && ok 'mutation applied to the staged copy' \
  || bad 'mutation anchor did not apply cleanly — nothing below is attributable'
if [[ $MUT_OK -eq 1 ]]; then
  MOUT="$(DROPSCAN_SHARED_UID=0 "$STAGE/scan.sh" scan --root "$LEAK" 2>&1)"; MRC=$?
  [[ $MRC -eq 0 ]] \
    && ok 'dulled scanner misses the planted secret — the section-2 catch pins this limb' \
    || bad "mutant still flagged (rc=$MRC); section 2 is not pinning secret-assignment" "$MOUT"
  COUT="$(DROPSCAN_SHARED_UID=0 "$TOOL" scan --root "$LEAK" 2>&1)"; CRC=$?
  [[ $CRC -eq 1 ]] \
    && ok 'control — the unmutated scanner still flags the same fixture' \
    || bad "unmutated scanner went quiet too (rc=$CRC); the miss is unattributable" "$COUT"
fi

hdr "6. This suite does not itself trip the CI Secret scan"
# Asserted with the scan's own regexes against this file, so the guard tracks
# the real gate rather than a paraphrase of it.
if grep -nIE '(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})' "${BASH_SOURCE[0]}" >/dev/null 2>&1; then
  bad 'this file contains a token-shaped literal; CI Secret scan will fail the job'
else
  ok 'no token-shaped literal committed in this suite'
fi
if grep -nIE 'BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY' "${BASH_SOURCE[0]}" >/dev/null 2>&1; then
  bad 'this file contains key-material-shaped text; CI Secret scan will fail the job'
else
  ok 'no key-material-shaped text committed in this suite'
fi
# Pairing guard: the regexes must actually be capable of matching, or the two
# checks above are vacuous and would pass against any file at all.
# The sentinel tail is fragmented for the same reason: a single 24+ char
# alnum literal would trip the tool's own entropy sweep when section 7 scans
# a copy of this file. No piece here reaches the sweep's 24-char floor.
TP="g""hp_"
TS1="ZZTESTSENTINEL"
TS2="DONOTPRINT0123"
TS3="456789abcdef"
SENT="${TP}${TS1}${TS2}${TS3}"
if grep -qIE '(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})' <<<"url=https://x:${SENT}@h/r.git"; then
  ok 'control — that regex does match the assembled sentinel, so the check is not vacuous'
else
  bad 'the Secret-scan regex matched nothing even against the sentinel; the check proves nothing'
fi

hdr "7. The tool scans a copy of this suite clean — and still catches a plant"
SELF="$WORK/selfscan"
mkdir -p "$SELF"; cp "${BASH_SOURCE[0]}" "$SELF/suite_copy.sh"
OUT="$(DROPSCAN_SHARED_UID=0 "$TOOL" scan --root "$SELF" 2>&1)"; RC=$?
[[ $RC -eq 0 ]] && ok 'a copy of this suite scans clean (no self-flagging)' \
  || bad "self-scan red (rc=$RC)" "$OUT"
printf '\n%s = %s\n' "$PLANTED_KEY" "$PLANTED_VAL" >> "$SELF/suite_copy.sh"
OUT="$(DROPSCAN_SHARED_UID=0 "$TOOL" scan --root "$SELF" 2>&1)"; RC=$?
[[ $RC -eq 1 ]] && ok 'a planted line in that same copy is caught (the clean above is not vacuous)' \
  || bad "planted line in suite copy went uncaught (rc=$RC)" "$OUT"

hdr "Result"
printf '  %d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
