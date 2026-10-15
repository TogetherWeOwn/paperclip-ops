#!/usr/bin/env bash
# Regression suite for gh_access.sh — dispatch refusal and policy purity (case_a063910b).
#
# THE BUG THIS EXISTS FOR. The catch-all arm of the dispatch case was
#
#   *) sed -n '...' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' | head -n -1 ;;
#
# so an unrecognised subcommand printed usage ON STDOUT and EXITED 0. A caller
# written the ordinary way —
#
#   tok="$(gh_access.sh tokne)" && use_credential "$tok"
#
# took the `&&`, with six lines of usage text in $tok. The failure was silent
# at the point it happened and surfaced somewhere unrelated. It is the exact
# case_7f0105f1 shape gh_token.sh already fixed: an unknown subcommand must refuse
# (usage on stderr, exit 2), never answer on stdout with exit 0.
#
# THE SECOND PROPERTY is policy purity. The `policy` arm seds the tool's own
# header — no backend, no DB, no container engine. This suite runs it with a
# PATH that contains no podman, docker, psql or paperclip CLI and proves it
# still exits 0 naming ELIGIBILITY and WHO MAY GRANT. If a future edit makes
# `policy` reach a backend, it fails here with "command not found" instead of
# hanging on a missing container in CI.
#
# WHAT IS ASSERTED, per refusal case:
#   exit status   exactly 2, not merely non-zero. Exit 1 is the preamble
#                 ("cannot read ...", "agent not found"); exit 2 is the usage
#                 refusal callers gate on. The mutant restores exit 0, so an
#                 assertion of "non-zero" alone would still catch it — but the
#                 exact-2 assertion is what names the refusal rather than any
#                 failure, and section 5 proves the mutant dies on it.
#   which stream  usage on stdout is the bug; usage on stderr is the fix. The
#                 caller's `$(...)` captures stdout, so "stdout is empty on a
#                 refusal" IS the user-visible property, asserted directly.
# Never the wording of the usage text. Message text is not a contract and
# pinning it makes ordinary edits red (precedent: test_gh_token_dispatch.sh).
#
# Offline by construction: COMPANY_ID=dummy satisfies the preamble, every run
# is under `env -i` with a restricted PATH, and no DB, container, network or
# credential is touched. `help`/`--help`/`-h` are deliberately NOT asserted:
# they are currently refused as unknown, and a future help arm (gh_token.sh
# precedent) must not be blocked by this suite.
#
# Requires bash, sed, grep. No container engine, no psql, no network.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Same override shape as test_gh_token_dispatch.sh, so a mutation guard can
# point this suite at a staged copy the same way.
TOOL="${GH_ACCESS_SH:-$HERE/gh_access.sh}"
PASS=0; FAIL=0

ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); [[ -n "${2:-}" ]] && printf '        %s\n' "$2"; }
hdr() { printf '\n\033[1m%s\033[0m\n' "$1"; }

for dep in sed grep; do
  command -v "$dep" >/dev/null 2>&1 || { echo "test_gh_access_dispatch: $dep is required" >&2; exit 2; }
done
[[ -r "$TOOL" ]] || { echo "test_gh_access_dispatch: cannot read $TOOL" >&2; exit 2; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- restricted PATH: core tools only, no backends -----------------------------
# The tool's policy/refusal paths need bash (shebang re-exec), dirname (HERE),
# sed and head (usage/policy extraction). Notably absent: podman, docker,
# psql, paperclipai, jq — any backend reach from the refusal or policy path
# fails closed here with "command not found".
RESTRICTED="$TMP/restricted-bin"
mkdir -p "$RESTRICTED"
for b in bash dirname sed head; do
  src="$(command -v "$b")" || { echo "test_gh_access_dispatch: $b is required" >&2; exit 2; }
  ln -s "$src" "$RESTRICTED/$b"
done

# --- runner --------------------------------------------------------------------
# `env -i` with COMPANY_ID=dummy: the preamble passes, so every assertion below
# executes the dispatch arm under test — not the "Set COMPANY_ID" death above
# it. ACCESS_LOG points at scratch so a future logging arm cannot write into
# the checkout.
RC=0; OUT=""; ERR=""
run_tool() { # run_tool <args...>
  OUT="$(env -i \
    PATH="$RESTRICTED" \
    HOME="$TMP" \
    TMPDIR="$TMP" \
    COMPANY_ID=dummy \
    ACCESS_LOG="$TMP/access.log" \
    "$BASH" "$TOOL" "$@" 2>"$TMP/stderr")"
  RC=$?
  ERR="$(cat "$TMP/stderr")"
}

# must_refuse <desc> <args...>
# The three assertions of a refusal. Each catches a different regression, and
# the stdout one is the bug this suite is named for.
must_refuse() {
  local desc="$1"; shift
  run_tool "$@"
  [[ $RC -eq 2 ]] \
    && ok "$desc: exit 2" \
    || bad "$desc: expected exit 2, got $RC — a caller's \`&&\` would proceed on exit 0" "rc=$RC out=[$(head -c 80 <<<"$OUT")]"
  [[ -z "$OUT" ]] \
    && ok "$desc: stdout empty, so \$(...) captures nothing usable" \
    || bad "$desc: WROTE $(wc -l <<<"$OUT") lines to stdout — a caller would use them as a value" "$OUT"
  [[ -n "$ERR" ]] \
    && ok "$desc: said why, on stderr" \
    || bad "$desc: refused silently — nothing on stderr"
}

printf '\033[1mgh_access.sh dispatch refusal and policy purity (case_a063910b)\033[0m\n'
printf 'tool: %s\n' "$TOOL"

# ------------------------------------------------------------------------------
hdr "1. The restricted PATH really has no backend (else section 2-3 prove nothing)"
for b in podman docker psql paperclipai; do
  if PATH="$RESTRICTED" command -v "$b" >/dev/null 2>&1; then
    bad "backend '$b' resolves under the restricted PATH — the purity proof is void"
  else
    ok "backend '$b' absent from the restricted PATH"
  fi
done
if PATH="$RESTRICTED" command -v sed >/dev/null 2>&1 \
   && PATH="$RESTRICTED" command -v dirname >/dev/null 2>&1; then
  ok "fixture: the restricted PATH still resolves the core tools the refusal path needs"
else
  bad "fixture: the restricted PATH cannot even resolve sed/dirname — sections below test nothing"
fi

# ------------------------------------------------------------------------------
hdr "2. An unrecognised subcommand must REFUSE, not print usage and succeed"
must_refuse "near-miss 'tokne' (the issue's example)"  tokne
must_refuse "near-miss 'polciy'"                        polciy
must_refuse "wrong case 'POLICY'"                       POLICY
must_refuse "plural 'shows'"                            shows
must_refuse "no subcommand at all"
must_refuse "--repos in the subcommand slot"            --repos foo
must_refuse "--version"                                 --version

hdr "3. Fuzz: subcommands nobody thought to allowlist"
for i in 1 2 3; do
  arg="zz$RANDOM$RANDOM"
  must_refuse "random subcommand '$arg' (#$i)" "$arg"
done

# ------------------------------------------------------------------------------
hdr "4. policy is pure: exit 0, names its contract, no backend needed"
run_tool policy
[[ $RC -eq 0 ]] \
  && ok "policy: exit 0 with no backend on PATH" \
  || bad "policy: expected exit 0, got $RC" "$ERR"
grep -q "ELIGIBILITY" <<<"$OUT" \
  && ok "policy: output names ELIGIBILITY" \
  || bad "policy: output does not name ELIGIBILITY" "$OUT"
grep -q "WHO MAY GRANT" <<<"$OUT" \
  && ok "policy: output names WHO MAY GRANT" \
  || bad "policy: output does not name WHO MAY GRANT" "$OUT"
[[ -z "$ERR" ]] \
  && ok "policy: nothing on stderr" \
  || bad "policy: wrote to stderr" "$ERR"

# ------------------------------------------------------------------------------
hdr "5. The caller pattern from the bug report, run literally"
used="$(env -i PATH="$RESTRICTED" HOME="$TMP" TMPDIR="$TMP" \
  COMPANY_ID=dummy ACCESS_LOG="$TMP/access.log" \
  "$BASH" -c 'tok="$("$0" tokne 2>/dev/null)" && printf "USED[%s]" "$tok"' "$TOOL")"
[[ -z "$used" ]] \
  && ok 'tok="$(gh_access.sh tokne)" && use "$tok" — the && is not taken' \
  || bad "the caller proceeded with: $(head -c 120 <<<"$used")"

used="$(env -i PATH="$RESTRICTED" HOME="$TMP" TMPDIR="$TMP" \
  COMPANY_ID=dummy ACCESS_LOG="$TMP/access.log" \
  "$BASH" -c 'out="$("$0" policy 2>/dev/null)" && printf "%s" "$out"' "$TOOL")"
if grep -q "ELIGIBILITY" <<<"$used" && grep -q "WHO MAY GRANT" <<<"$used"; then
  ok 'out="$(gh_access.sh policy)" && use "$out" — still works'
else
  bad "the real policy idiom broke: got [$(head -c 60 <<<"$used")]"
fi

# ------------------------------------------------------------------------------
hdr "6. MUTATION: restoring the exit-0 fallthrough must cost the refusal (case_b04b43a0)"
# Base-run rule: the unmutated tool must refuse the SAME fixture in the SAME
# staging layout first, or the mutant's red below is unattributable. The staged
# copy carries its own lib/ because the tool sources lib/pcsql.sh relative to
# itself; without it the mutant would die on a missing source file and look
# "detected" having tested nothing.
STAGE="$TMP/mutant"
mkdir -p "$STAGE/lib"
cp "$TOOL" "$STAGE/gh_access.sh"
cp "$HERE/lib/pcsql.sh" "$STAGE/lib/pcsql.sh"
chmod +x "$STAGE/gh_access.sh"
MUT_OK=1
OLD='  *) refuse_usage "${1:-}" ;;'
NEW='  *) usage ;;'
OLD="$OLD" NEW="$NEW" F="$STAGE/gh_access.sh" python3 - <<'PY' || MUT_OK=0
import os
f = os.environ["F"]; old = os.environ["OLD"]; new = os.environ["NEW"]
s = open(f).read()
assert s.count(old) == 1, f"anchor appears {s.count(old)} times, expected 1"
open(f, "w").write(s.replace(old, new))
PY
[[ $MUT_OK -eq 1 ]] && ok 'mutation applied to the staged copy' \
  || bad 'mutation anchor did not apply cleanly — nothing below is attributable'
if [[ $MUT_OK -eq 1 ]]; then
  MOUT="$(env -i PATH="$RESTRICTED" HOME="$TMP" TMPDIR="$TMP" \
    COMPANY_ID=dummy ACCESS_LOG="$TMP/access.log" \
    "$BASH" "$STAGE/gh_access.sh" tokne 2>"$TMP/mut-stderr")"; MRC=$?
  MUTERR="$(cat "$TMP/mut-stderr")"
  if [[ $MRC -eq 0 && -n "$MOUT" && -z "$MUTERR" ]]; then
    ok 'mutant restores the bug: exit 0 with usage on stdout, nothing on stderr'
  else
    bad "mutant did not restore the bug (rc=$MRC stdout_bytes=${#MOUT} stderr_bytes=${#MUTERR}) — the proof is void" "$MUTERR"
  fi
  COUT="$(env -i PATH="$RESTRICTED" HOME="$TMP" TMPDIR="$TMP" \
    COMPANY_ID=dummy ACCESS_LOG="$TMP/access.log" \
    "$BASH" "$TOOL" tokne 2>/dev/null)"; CRC=$?
  if [[ $CRC -eq 2 && -z "$COUT" ]]; then
    ok 'control — the unmutated tool still refuses the same fixture (exit 2, stdout empty)'
  else
    bad "unmutated tool went quiet too (rc=$CRC); the mutant red is unattributable" "$COUT"
  fi
fi

printf '\n\033[1mRESULT: %d passed, %d failed\033[0m\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
