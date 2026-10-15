#!/usr/bin/env bash
# Regression suite for public_comment_hygiene_gate.sh  (case_99462e9c).
#
# Runs fully offline: every case writes a small candidate body to a temp file
# and runs the gate against it. No network, no credentials, no fixtures.
#
# The two failures this suite is built to catch, both in the fail-open
# direction -- the direction that would post another internal ID to a public
# repo the way two-bot-next#398 and #129 got theirs:
#
#   * a gate that never fires (always exit 0). The positive control -- the
#     literal "Fixes belong to the owner of case_fa4fcb64" sentence shape from the
#     #398 comments -- must exit 1 on public, or the gate is decoration.
#   * a gate that fires on private repos. Internal IDs belong in private text
#     (`Refs: case_1cab5f7f`); a gate that blocks them there would train reviewers
#     to bypass the gate everywhere, including public.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="$HERE/public_comment_hygiene_gate.sh"
CLEAN_PATH="/usr/bin:/bin"
ENV_BIN="$(PATH="$CLEAN_PATH" type -P env)"
BASH_BIN="$(PATH="$CLEAN_PATH" type -P bash)"
PASS=0; FAIL=0
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [[ ! -x "$GATE" ]]; then
  echo "ERROR: $GATE not found or not executable" >&2
  exit 2
fi
if [[ -z "$ENV_BIN" || -z "$BASH_BIN" ]]; then
  echo "ERROR: env and bash are required on $CLEAN_PATH" >&2
  exit 2
fi
chmod +x "$GATE"

# Gate subprocesses must not inherit exported functions or BASH_ENV from the
# shell running this suite. Only the scanner-failure cases vary PATH, and that
# path is still built from the known system directories above.
run_gate() {
  "$ENV_BIN" -i "PATH=$CLEAN_PATH" "$BASH_BIN" --noprofile --norc "$GATE" "$@"
}
run_gate_with_path() {
  local path="$1"; shift
  "$ENV_BIN" -i "PATH=$path" "$BASH_BIN" --noprofile --norc "$GATE" "$@"
}
run_gate_with_prefixes() {
  local prefixes="$1"; shift
  "$ENV_BIN" -i "PATH=$CLEAN_PATH" "INTERNAL_ID_PREFIXES=$prefixes" \
    "$BASH_BIN" --noprofile --norc "$GATE" "$@"
}

ok()  { PASS=$((PASS+1)); printf 'ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL %s\n' "$1"; [[ -n "${2:-}" ]] && printf '     %s\n' "$2"; }

# check <expected_exit> <label> <body-text> [extra gate args...]
check() {
  local expected="$1" label="$2" text="$3"; shift 3
  local f="$WORK/body.txt"
  printf '%s' "$text" > "$f"
  local out rc
  out="$(run_gate --body-file "$f" "$@" 2>&1)"; rc=$?
  if [[ "$rc" == "$expected" ]]; then ok "$label (exit $rc)";
  else bad "$label" "expected exit $expected, got $rc :: $out"; fi
}

LEAK="Review abc1234: CHANGES. Fixes belong to the owner of case_fa4fcb64; re-check here after repair."
CLEAN="Review abc1234: CHANGES. Fixes belong to the PR author; re-check here after repair."

# --- 1. positive control: the evidenced failure shape must fire on public ---
check 1 "public body with TOG-ID fires" "$LEAK" --visibility public

# --- 2. same text on private passes (IDs belong there) ---
check 0 "private body with TOG-ID passes" "$LEAK" --visibility private

# --- 3. scrubbed shape passes on public (the fix this gate protects) ---
check 0 "public scrubbed body passes" "$CLEAN" --visibility public

# --- 4. empty body passes on public (nothing to leak) ---
check 0 "public empty body passes" "" --visibility public

# --- 5. PAP- prefix fires (pr-lint prefix set, not just TOG-) ---
check 1 "public body with PAP-ID fires" "See case_34ed41ba for context." --visibility public

# --- 6. word boundaries: TOGETHERWEOWN and PAPER are not IDs ---
check 0 "public body with TOGETHERWEOWN passes" "From togetherweown[bot] via paper trail." --visibility public

# --- 7. branch names are checked too when public ---
BRF="$WORK/branch.txt"; printf '%s' "$CLEAN" > "$BRF"
if run_gate --visibility public --body-file "$BRF" --branch "fix/case_99462e9c-scrub" >/dev/null 2>&1; then
  bad "public TOG branch fires" "expected exit 1, got 0"
else
  [[ $? == 1 ]] && ok "public TOG branch fires (exit 1)" || bad "public TOG branch fires" "expected exit 1, got $?"
fi
if run_gate --visibility public --body-file "$BRF" --branch "fix/scrub-bot-comments" >/dev/null 2>&1; then
  ok "public clean branch passes (exit 0)"
else bad "public clean branch passes" "expected exit 0, got $?"; fi
if run_gate --visibility private --body-file "$BRF" --branch "fix/case_99462e9c-scrub" >/dev/null 2>&1; then
  ok "private TOG branch passes (exit 0)"
else bad "private TOG branch passes" "expected exit 0, got $?"; fi

# --- 8. usage errors exit 2, never 0 or 1 ---
if run_gate --visibility public --body-file "$WORK/nope.txt" >/dev/null 2>&1; then
  bad "missing body file is usage error" "expected exit 2, got 0"
else
  [[ $? == 2 ]] && ok "missing body file is usage error (exit 2)" || bad "missing body file is usage error" "expected exit 2, got $?"
fi
printf 'x' > "$WORK/x.txt"
if run_gate --visibility staging --body-file "$WORK/x.txt" >/dev/null 2>&1; then
  bad "bad visibility is usage error" "expected exit 2, got 0"
else
  [[ $? == 2 ]] && ok "bad visibility is usage error (exit 2)" || bad "bad visibility is usage error" "expected exit 2, got $?"
fi

# --- 9. scanner failures and non-file inputs exit 2, never 0 (case_a77f9c52) ---
# A scanner error must never read as "no matches": the old `|| true` mapped
# grep exit >1 to a clean PASS. Each scan path is pinned separately.
printf '%s' "$CLEAN" > "$WORK/clean9.txt"

# 9a. malformed pattern breaks the body scan -> 2, not PASS.
if run_gate_with_prefixes '[' --visibility public --body-file "$WORK/clean9.txt" >/dev/null 2>&1; then
  bad "malformed pattern is scanner error" "expected exit 2, got 0"
else
  [[ $? == 2 ]] && ok "malformed pattern is scanner error (exit 2)" || bad "malformed pattern is scanner error" "expected exit 2, got $?"
fi

# 9b. injected body-scanner failure -> 2, not PASS.
mkdir -p "$WORK/shim_all"
printf '#!/usr/bin/env bash\necho "simulated grep failure" >&2\nexit 2\n' > "$WORK/shim_all/grep"
chmod +x "$WORK/shim_all/grep"
if run_gate_with_path "$WORK/shim_all:$CLEAN_PATH" --visibility public --body-file "$WORK/clean9.txt" >/dev/null 2>&1; then
  bad "body scanner error is error" "expected exit 2, got 0"
else
  [[ $? == 2 ]] && ok "body scanner error is error (exit 2)" || bad "body scanner error is error" "expected exit 2, got $?"
fi

# 9c. branch-path scanner failure -> 2, not PASS. The body scan uses the
# combined -nE option; delegate option tokens containing n and fail the branch
# scan, which has no -n option, so a clean body cannot mask the failure.
REAL_GREP="$(PATH="$CLEAN_PATH" type -P grep)"
mkdir -p "$WORK/shim_branch"
printf '#!/usr/bin/env bash\nfor a in "$@"; do if [[ "$a" == -*n* ]]; then exec "%s" "$@"; fi; done\necho "simulated branch-scan failure" >&2\nexit 2\n' "$REAL_GREP" > "$WORK/shim_branch/grep"
chmod +x "$WORK/shim_branch/grep"
if run_gate_with_path "$WORK/shim_branch:$CLEAN_PATH" --visibility public --body-file "$WORK/clean9.txt" --branch "fix/scrub-bot-comments" >/dev/null 2>&1; then
  bad "branch scanner error is error" "expected exit 2, got 0"
else
  [[ $? == 2 ]] && ok "branch scanner error is error (exit 2)" || bad "branch scanner error is error" "expected exit 2, got $?"
fi

# 9d. readable directory is not a file -> 2, not PASS.
mkdir -p "$WORK/adir"
if run_gate --visibility public --body-file "$WORK/adir" >/dev/null 2>&1; then
  bad "directory body is usage error" "expected exit 2, got 0"
else
  [[ $? == 2 ]] && ok "directory body is usage error (exit 2)" || bad "directory body is usage error" "expected exit 2, got $?"
fi

# 9e. unreadable file -> 2, not PASS.
printf '%s' "$CLEAN" > "$WORK/noperm.txt"
chmod 000 "$WORK/noperm.txt"
if run_gate --visibility public --body-file "$WORK/noperm.txt" >/dev/null 2>&1; then
  bad "unreadable body is usage error" "expected exit 2, got 0"
else
  [[ $? == 2 ]] && ok "unreadable body is usage error (exit 2)" || bad "unreadable body is usage error" "expected exit 2, got $?"
fi
chmod 644 "$WORK/noperm.txt"

# 9f. dangling symlink -> 2, not PASS.
ln -s "$WORK/does-not-exist.txt" "$WORK/dangling.txt"
if run_gate --visibility public --body-file "$WORK/dangling.txt" >/dev/null 2>&1; then
  bad "dangling symlink body is usage error" "expected exit 2, got 0"
else
  [[ $? == 2 ]] && ok "dangling symlink body is usage error (exit 2)" || bad "dangling symlink body is usage error" "expected exit 2, got $?"
fi

echo
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
