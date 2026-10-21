#!/usr/bin/env bash
# test_mutmut_changed.sh -- fixture test for scripts/quality-gates/mutmut_changed.sh
# (mutmut-on-changed-lines gate, WARN).
#
# Proves the budget-kill path leaves the calling checkout clean: the gate runs
# mutmut in a throwaway worktree, so when `timeout` SIGTERMs mutmut mid-mutant
# (rc 124) the in-flight mutant and .mutmut-cache die with the worktree and no
# later gate ever reads a mutated file. A stub `mutmut` module mutates
# mod.py in place and then sleeps past a tiny budget; hermetic, no network.
# Exit 0 all green, 1 anything red.
set -uo pipefail

# Isolate the fixture from inherited Git environment: a caller exporting
# GIT_DIR (hooks, other fixtures) would redirect `git -C "$REPO"` into the
# caller's own repository instead of the fixture.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_NAMESPACE

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
GATE="$ROOT/scripts/quality-gates/mutmut_changed.sh"
TMP=$(mktemp -d)
mkdir -p "$TMP/t"
trap 'rm -rf "$TMP"' EXIT

pass=0
fail=0
ok() { printf 'ok - %s\n' "$1"; pass=$((pass + 1)); }
not_ok() { printf 'not ok - %s\n' "$1"; printf '%s\n' "$2"; fail=$((fail + 1)); }

# --- fixture repo: one changed non-test .py file, base -> HEAD ---
REPO="$TMP/repo"
mkdir -p "$REPO/scripts/quality-gates"
cp "$GATE" "$REPO/scripts/quality-gates/"
printf 'VALUE = 1\n' > "$REPO/mod.py"
git -C "$REPO" init -q
if [[ "$(git -C "$REPO" rev-parse --absolute-git-dir)" != "$REPO/.git" ]]; then
  echo "fixture git commands escaped the fixture repo -- aborting" >&2
  exit 1
fi
git -C "$REPO" -c user.email=t@t -c user.name=t add -A
git -C "$REPO" -c user.email=t@t -c user.name=t commit -qm base
BASE=$(git -C "$REPO" rev-parse HEAD)
printf 'VALUE = 2\n' > "$REPO/mod.py"
git -C "$REPO" -c user.email=t@t -c user.name=t commit -qam head
EXPECTED=$(cat "$REPO/mod.py")

# --- stub mutmut: `run` mutates cwd/mod.py in place then hangs; the
# results commands stay silent. PYTHONPATH puts the stub ahead of any real
# mutmut install, and the stub ignores all argv by design. ---
STUB="$TMP/stubbin/mutmut"
mkdir -p "$STUB"
touch "$STUB/__init__.py"
cat > "$STUB/__main__.py" <<'PY'
import sys
import time
from pathlib import Path

cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "run":
    Path("mod.py").write_text("VALUE = 999  # mutant\n")
    time.sleep(120)
PY

OUT="$TMP/out"
# NOTE: GITHUB_EVENT_NAME unset + BASE_SHA set selects the diff path;
# MUTMUT_BUDGET_SECS=3 forces the rc-124 budget-kill branch quickly.
rc=0
env -u GITHUB_EVENT_NAME \
  BASE_SHA="$BASE" MUTMUT_BUDGET_SECS=3 TMPDIR="$TMP/t" RUNNER_TEMP="$TMP/t" \
  PYTHONPATH="$TMP/stubbin" \
  bash "$REPO/scripts/quality-gates/mutmut_changed.sh" >"$OUT" 2>&1 || rc=$?

if [[ "$rc" -eq 0 ]]; then
  ok "gate exits 0 on the budget-kill path (WARN, never fails)"
else
  not_ok "gate exits 0 on the budget-kill path (WARN, never fails)" "$OUT"
fi

if grep -q "budget (3s) expired" "$OUT"; then
  ok "budget expiry is reported as a warning"
else
  not_ok "budget expiry is reported as a warning" "$OUT"
fi

if [[ "$(cat "$REPO/mod.py")" == "$EXPECTED" ]]; then
  ok "calling checkout keeps its HEAD content (no mutant left behind)"
else
  not_ok "calling checkout keeps its HEAD content (no mutant left behind)" \
    "$(cat "$REPO/mod.py")"
fi

if [[ -z "$(git -C "$REPO" status --porcelain)" ]]; then
  ok "git status of the calling checkout is empty"
else
  not_ok "git status of the calling checkout is empty" \
    "$(git -C "$REPO" status --porcelain)"
fi

if [[ ! -e "$REPO/.mutmut-cache" ]]; then
  ok "no .mutmut-cache leaks into the calling checkout"
else
  not_ok "no .mutmut-cache leaks into the calling checkout" "$OUT"
fi

wt_list="$(git -C "$REPO" worktree list --porcelain)"
if ! grep -q "mutmut-wt" <<<"$wt_list"; then
  ok "throwaway worktree is removed on exit"
else
  not_ok "throwaway worktree is removed on exit" \
    "$wt_list"
fi

printf 'pass=%d fail=%d\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
