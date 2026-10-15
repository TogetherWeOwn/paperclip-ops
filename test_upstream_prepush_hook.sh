#!/usr/bin/env bash
# ===========================================================================
# Offline suite for upstream_prepush_hook.sh (case_1ffacb90, roadmap item 2.14).
#
# The hook is the last automated guard before a push goes public outside
# TogetherWeOwn. test_upstream_lint.py already covers it with a 17-test Hook
# class using real git and the real linter; this suite is the offline
# complement (stub git via GIT_BIN plus a stub python3 linter — no network,
# no credentials, no live repo) pinning the hook's own fail-closed limbs and
# the linter argv wiring (slug, branch, title, range, body file, stage, live).
#
# WHY STUBS, NOT LIVE PUSHES. The hook shells `git` for local reads only
# (rev-parse, merge-base, rev-list) and `python3 upstream_lint.py` for the
# verdict; the remote itself is never contacted during the lint. So `git` is
# stubbed through GIT_BIN (the hook's own seam) and `python3` is stubbed on
# PATH to emulate the linter verdict (recording its argv, exiting
# STUB_LINT_RC). The remotes under test are fake URL strings. Nothing is
# pushed anywhere; an unexpected stub invocation fails loudly (exit 99),
# never green.
#
# THE GREEN CONTROL IS LOAD-BEARING. A hook that always exits 1 would pass
# every red case below, so section 5 (valid external push, passing linter)
# must exit 0 with the linter argv proving the wiring (slug, branch, title,
# full-PR range, body file, stage, live), and section 6 (failing linter)
# must still refuse. Section 7 deletes
# the final `block` call and requires the section-2 catch to redden.
#
# BASH_ENV resets PATH in every non-interactive bash (sandbox shim trap), so
# a child would lose the python3 stub and invoke the REAL linter (network,
# maintainer memory) instead. Unset it so the stub holds, for the same reason
# test_acceptance_transport.sh does.
# ===========================================================================
unset BASH_ENV
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$ROOT/upstream_prepush_hook.sh"
[ -x "$HOOK" ] || { echo "no executable upstream_prepush_hook.sh at $HOOK" >&2; exit 2; }
command -v python3 >/dev/null || { echo "ERROR: python3 required" >&2; exit 1; }
REAL_PYTHON3="$(command -v python3)"
export REAL_PYTHON3

PASS=0; FAIL=0
ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; [ $# -gt 1 ] && printf '        %s\n' "$2"; FAIL=$((FAIL+1)); }
hdr() { printf '\n\033[1m%s\033[0m\n' "$1"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/bin"; mkdir -p "$BIN"

# --- stub git (via the hook's GIT_BIN seam) -----------------------------------
cat > "$BIN/git" <<'STUB'
#!/usr/bin/env bash
# Emulates only the reads upstream_prepush_hook.sh makes. Anything else is a
# loud failure (exit 99), never a silent green.
set -uo pipefail
if [[ "${1:-}" == "rev-parse" && "${2:-}" == "--git-dir" ]]; then
  printf '%s' "${STUB_GITDIR:?STUB_GITDIR unset}"; exit 0
fi
if [[ "${1:-}" == "rev-parse" && "${2:-}" == "-q" ]]; then
  # hook form: rev-parse -q --verify <ref>
  [[ "${STUB_HAS_BASE:-0}" == "1" ]] && exit 0 || exit 1
fi
if [[ "${1:-}" == "merge-base" ]]; then
  if [[ "${STUB_HAS_BASE:-0}" == "1" ]]; then
    printf '%s' "${STUB_BASE_SHA:?STUB_BASE_SHA unset}"; exit 0
  fi
  exit 1
fi
if [[ "${1:-}" == "rev-list" ]]; then
  printf '1'; exit 0
fi
echo "stub git: unexpected invocation: $*" >&2; exit 99
STUB
chmod +x "$BIN/git"

# --- stub python3: emulate the linter verdict, delegate everything else -------
cat > "$BIN/python3" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
for a in "$@"; do
  if [[ "$a" == *upstream_lint.py* ]]; then
    printf '%s\n' "$*" >> "${STUB_LINT_LOG:?STUB_LINT_LOG unset}"
    exit "${STUB_LINT_RC:-0}"
  fi
done
exec "${REAL_PYTHON3:?REAL_PYTHON3 unset}" "$@"
STUB
chmod +x "$BIN/python3"

export GIT_BIN="$BIN/git"
export PATH="$BIN:$PATH"

ZERO40="0000000000000000000000000000000000000000"
LOCAL_SHA="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
BASE_SHA="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
BRANCH="feat-x"
EXT_URL="https://github.com/example-org/example-repo.git"
TWO_URL="https://github.com/TogetherWeOwn/some-repo.git"
TWO_URL_LOWER="person-dd677eb8@example.invalid:togetherweown/some-repo.git"
FOREIGN_URL="https://example.com/owner/repo.git"
TITLE1="feat(scope): add offline pre-push hook test"

export STUB_BASE_SHA="$BASE_SHA"
export STUB_LINT_LOG="$WORK/lint.log"

# fresh fixture gitdir; with_text=1 writes title.txt + body.md under
# upstream-pr/<branch>/, the layout the hook reads.
mk_gitdir() { # <name> <with_text:0|1>
  local d="$WORK/$1/gitdir" t
  mkdir -p "$d"
  if [[ "$2" == "1" ]]; then
    t="$d/upstream-pr/$BRANCH"; mkdir -p "$t"
    printf '%s\n' "$TITLE1" > "$t/title.txt"
    printf '# What changed\n\n- offline hook test\n' > "$t/body.md"
  fi
  printf '%s' "$d"
}

push_input() { # <file>: one updated-branch line on a non-TWO remote
  printf '%s %s %s %s\n' "refs/heads/$BRANCH" "$LOCAL_SHA" "refs/heads/$BRANCH" "$ZERO40" > "$1"
}

run_hook() { # <remote-name> <remote-url> [stdin-file]
  local input="${3:-/dev/null}"
  local out="$WORK/hook.out" err="$WORK/hook.err"
  : > "$STUB_LINT_LOG"
  set +e
  "$HOOK" "$1" "$2" <"$input" >"$out" 2>"$err"
  HOOK_RC=$?
  set -e
  HOOK_OUT="$(cat "$out")"; HOOK_ERR="$(cat "$err")"
}

# expect_block <fragment> <test-name>: stderr names the hook's one-line
# BLOCKED reason. The fragment must sit on exactly one line, so a multi-line
# dump cannot smuggle it.
expect_block() {
  local frag="$1" name="$2" n
  if grep -qF "$frag" <<<"$HOOK_ERR"; then
    ok "$name (stderr names '$frag')"
  else
    bad "$name (stderr does not name '$frag')" "$HOOK_ERR"; return
  fi
  n="$(grep -cF "$frag" <<<"$HOOK_ERR" || true)"
  if [[ "$n" -eq 1 ]]; then
    ok "$name reason is one line"
  else
    bad "$name reason spans $n lines" "$HOOK_ERR"
  fi
}

nonempty_lines() { grep -c . <<<"$1" || true; }

# ===========================================================================
hdr "1. TogetherWeOwn remote passes straight through (exit 0, linter silent)"
# Discriminating fixture: a pushed branch with a resolvable base but no PR
# text. The real hook exits 0 before reading either; a hook with the
# passthrough deleted proceeds and refuses with 'no PR text' (rc 1).
export STUB_HAS_BASE=1 STUB_LINT_RC=0
STUB_GITDIR="$(mk_gitdir case1 0)"
export STUB_GITDIR
push_input "$WORK/in1"
run_hook origin "$TWO_URL" "$WORK/in1"
[[ "$HOOK_RC" -eq 0 ]] && ok "TogetherWeOwn push exits 0" || bad "TogetherWeOwn push exited $HOOK_RC" "$HOOK_ERR"
[[ -z "$HOOK_ERR" ]] && ok "passthrough prints no stderr" || bad "passthrough printed stderr" "$HOOK_ERR"
if [[ -s "$STUB_LINT_LOG" ]]; then
  bad "passthrough invoked the linter" "$(cat "$STUB_LINT_LOG")"
else
  ok "passthrough never invokes the linter"
fi
# Lower-case owner URL pins the case fold both directions: an exact-case
# mutant ('TogetherWeOwn' only) fails here, a fold-deletion mutant fails above.
push_input "$WORK/in1b"
run_hook origin "$TWO_URL_LOWER" "$WORK/in1b"
[[ "$HOOK_RC" -eq 0 ]] && ok "lower-case TogetherWeOwn push exits 0" || bad "lower-case TogetherWeOwn push exited $HOOK_RC" "$HOOK_ERR"
[[ -z "$HOOK_ERR" ]] && ok "lower-case passthrough prints no stderr" || bad "lower-case passthrough printed stderr" "$HOOK_ERR"
if [[ -s "$STUB_LINT_LOG" ]]; then
  bad "lower-case passthrough invoked the linter" "$(cat "$STUB_LINT_LOG")"
else
  ok "lower-case passthrough never invokes the linter"
fi

# ===========================================================================
hdr "2. Missing PR text blocks with a one-line reason"
export STUB_HAS_BASE=1 STUB_LINT_RC=0
STUB_GITDIR="$(mk_gitdir case2a 0)"
export STUB_GITDIR
push_input "$WORK/in2a"
run_hook origin "$EXT_URL" "$WORK/in2a"
[[ "$HOOK_RC" -eq 1 ]] && ok "neither title nor body exits 1" || bad "neither title nor body exited $HOOK_RC" "$HOOK_ERR"
expect_block "no PR text for '$BRANCH'" "missing PR text"

# Title alone is not enough: an empty body must still refuse (-s, not -e).
STUB_GITDIR="$(mk_gitdir case2b 0)"
export STUB_GITDIR
mkdir -p "$STUB_GITDIR/upstream-pr/$BRANCH"
printf '%s\n' "$TITLE1" > "$STUB_GITDIR/upstream-pr/$BRANCH/title.txt"
: > "$STUB_GITDIR/upstream-pr/$BRANCH/body.md"
push_input "$WORK/in2b"
run_hook origin "$EXT_URL" "$WORK/in2b"
[[ "$HOOK_RC" -eq 1 ]] && ok "empty body exits 1" || bad "empty body exited $HOOK_RC" "$HOOK_ERR"
expect_block "no PR text for '$BRANCH'" "empty body"

# Body alone is not enough: a missing/empty title must still refuse. Both
# cases above are already refused by the body check, so without this case a
# hook with the title limb deleted stays green.
STUB_GITDIR="$(mk_gitdir case2c 0)"
export STUB_GITDIR
mkdir -p "$STUB_GITDIR/upstream-pr/$BRANCH"
: > "$STUB_GITDIR/upstream-pr/$BRANCH/title.txt"
printf '# What changed\n\n- offline hook test\n' > "$STUB_GITDIR/upstream-pr/$BRANCH/body.md"
push_input "$WORK/in2c"
run_hook origin "$EXT_URL" "$WORK/in2c"
[[ "$HOOK_RC" -eq 1 ]] && ok "empty title exits 1" || bad "empty title exited $HOOK_RC" "$HOOK_ERR"
expect_block "no PR text for '$BRANCH'" "empty title"

# ===========================================================================
hdr "3. Unknown remote host blocks with a one-line reason"
export STUB_HAS_BASE=0
run_hook origin "$FOREIGN_URL"
[[ "$HOOK_RC" -eq 1 ]] && ok "foreign host exits 1" || bad "foreign host exited $HOOK_RC" "$HOOK_ERR"
expect_block "not a recognizable github.com repo" "unknown remote host"
if [[ "$(nonempty_lines "$HOOK_ERR")" -eq 1 ]]; then
  ok "unknown-host refusal is exactly one line on stderr"
else
  bad "unknown-host stderr is not one line" "$HOOK_ERR"
fi

# ===========================================================================
hdr "4. Unresolvable base blocks, even with valid PR text"
export STUB_HAS_BASE=0 STUB_LINT_RC=0
STUB_GITDIR="$(mk_gitdir case4 1)"
export STUB_GITDIR
push_input "$WORK/in4"
run_hook origin "$EXT_URL" "$WORK/in4"
[[ "$HOOK_RC" -eq 1 ]] && ok "missing base exits 1" || bad "missing base exited $HOOK_RC" "$HOOK_ERR"
expect_block "cannot resolve the full PR base of '$BRANCH'" "unresolvable base"
if grep -qF "no PR text" <<<"$HOOK_ERR"; then
  bad "base refusal is masked by a PR-text refusal" "$HOOK_ERR"
else
  ok "base is checked before PR text"
fi

# ===========================================================================
hdr "5. GREEN CONTROL: valid external push with a passing linter exits 0"
export STUB_HAS_BASE=1 STUB_LINT_RC=0
STUB_GITDIR="$(mk_gitdir case5 1)"
export STUB_GITDIR
push_input "$WORK/in5"
run_hook origin "$EXT_URL" "$WORK/in5"
[[ "$HOOK_RC" -eq 0 ]] && ok "valid push exits 0" || bad "valid push exited $HOOK_RC" "$HOOK_ERR"
if grep -qF "linting '$BRANCH'" <<<"$HOOK_OUT"; then
  ok "hook reaches the linter for a valid push"
else
  bad "hook never reached the linter" "$HOOK_OUT"
fi
for want in "--repo example-org/example-repo" "--branch $BRANCH" "$TITLE1" "--stage first-push" "--git-range $BASE_SHA..$LOCAL_SHA" "--live" "--body-file $STUB_GITDIR/upstream-pr/$BRANCH/body.md"; do
  if grep -qF -- "$want" "$STUB_LINT_LOG"; then
    ok "linter argv carries '$want'"
  else
    bad "linter argv misses '$want'" "$(cat "$STUB_LINT_LOG")"
  fi
done

# ===========================================================================
hdr "6. FAIL-CLOSED CONTROL: a refusing linter blocks the push"
export STUB_HAS_BASE=1 STUB_LINT_RC=1
STUB_GITDIR="$(mk_gitdir case6 1)"
export STUB_GITDIR
push_input "$WORK/in6"
run_hook origin "$EXT_URL" "$WORK/in6"
[[ "$HOOK_RC" -eq 1 ]] && ok "refusing linter exits 1" || bad "refusing linter exited $HOOK_RC" "$HOOK_ERR"
expect_block "pre-push lint failed" "failing linter"

# ===========================================================================
hdr "7. MUTATION: deleting the final block call must cost the section-2 catch"
STAGE="$WORK/mutant"
mkdir -p "$STAGE"; cp "$HOOK" "$STAGE/hook.sh"; chmod +x "$STAGE/hook.sh"
MUT_OK=1
OLD='|| block "pre-push lint failed'
NEW='|| true  # MUTANT: fail-open (deleted block call)'
OLD="$OLD" NEW="$NEW" F="$STAGE/hook.sh" "$REAL_PYTHON3" - <<'PY' || MUT_OK=0
import os
f = os.environ["F"]; old = os.environ["OLD"]; new = os.environ["NEW"]
s = open(f).read()
assert s.count(old) == 1, "anchor appears %d times, expected 1" % s.count(old)
open(f, "w").write(s.replace(old, new))
PY
[[ "$MUT_OK" -eq 1 ]] && ok "mutation applied to the staged copy" \
  || bad "mutation anchor did not apply cleanly — nothing below is attributable"
if [[ "$MUT_OK" -eq 1 ]]; then
  REAL_HOOK="$HOOK"
  export STUB_HAS_BASE=1 STUB_LINT_RC=0
  STUB_GITDIR="$(mk_gitdir casem 0)"
  export STUB_GITDIR
  push_input "$WORK/inm"
  HOOK="$STAGE/hook.sh"
  run_hook origin "$EXT_URL" "$WORK/inm"
  HOOK="$REAL_HOOK"
  if [[ "$HOOK_RC" -eq 0 ]]; then
    ok "mutant misses the missing-text block — the section-2 catch pins this limb"
  else
    bad "mutant still blocked (rc=$HOOK_RC); section 2 is not pinning the final block call" "$HOOK_ERR"
  fi
  run_hook origin "$EXT_URL" "$WORK/inm"
  if [[ "$HOOK_RC" -eq 1 ]] && grep -qF "no PR text for '$BRANCH'" <<<"$HOOK_ERR"; then
    ok "control — the unmutated hook still blocks the same fixture"
  else
    bad "unmutated hook went quiet too (rc=$HOOK_RC); the miss is unattributable" "$HOOK_ERR"
  fi
fi

hdr "Result"
printf '  %d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]] || exit 1
