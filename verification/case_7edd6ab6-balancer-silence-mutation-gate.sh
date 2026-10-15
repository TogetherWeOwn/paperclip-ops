#!/usr/bin/env bash
# ===========================================================================
# case_7edd6ab6-balancer-silence-mutation-gate.sh — proof that
# test_balancer_silence_guard.py is not vacuous.
# ---------------------------------------------------------------------------
# A green suite is not evidence until it can be shown to go red. This gate
# breaks one limb of balancer_silence_guard.py at a time and asserts the
# suite catches each break. The mutants are the ways this guard could
# silently become a false green:
#
#   * a threshold softened from `>` to `>=` (freshness and gap alike)
#   * a stale verdict reported as fresh (the 193-min silence scores green)
#   * an unmeasurable log falling through instead of refusing
#
# A mutation that does not change the file is a FAILED gate, not a passing
# one: a sed that silently matched nothing would otherwise score as a kill.
#
# Exit 0 only if the baseline is green AND every mutant is killed.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SRC="$ROOT/balancer_silence_guard.py"
SUITE="$ROOT/test_balancer_silence_guard.py"
PASS=0; FAIL=0

ok()  { printf '  \033[32m  PASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31m  FAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
hdr() { printf '\n\033[1m%s\033[0m\n' "$1"; }

[ -f "$SRC" ]   || { echo "FATAL: cannot find balancer_silence_guard.py above $HERE" >&2; exit 4; }
[ -f "$SUITE" ] || { echo "FATAL: cannot find test_balancer_silence_guard.py above $HERE" >&2; exit 4; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/case_05fb272f_silence_mutation.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT

stage() {
  local dest="$1"
  mkdir -p "$dest"
  cp "$SRC" "$dest/balancer_silence_guard.py"
  cp "$SUITE" "$dest/test_balancer_silence_guard.py"
}

hdr "=== baseline: the UNMUTATED copy passes in this same staging directory ==="
BASE="$WORK/baseline"
stage "$BASE"
if (cd "$BASE" && python3 -m unittest test_balancer_silence_guard.py) > "$WORK/baseline.log" 2>&1; then
  ok "unmutated copy: suite green in $BASE"
else
  bad "unmutated copy FAILED — the gate cannot attribute anything to a mutation"
  tail -20 "$WORK/baseline.log"
  printf '\npassed %d, failed %d\n' "$PASS" "$FAIL"
  exit 1
fi

# mutate <description> <old> <new>
n=0
mutate() {
  local desc="$1" old="$2" new="$3"
  n=$((n+1))
  local dir="$WORK/mut$n"
  stage "$dir"
  # Mutate only NON-COMMENT, NON-DOCSTRING lines: the header prose mentions
  # `>` and `>=` deliberately, and mutating prose proves nothing.
  local rc
  OLD="$old" NEW="$new" python3 - "$dir/balancer_silence_guard.py" <<'PY'
import os, sys
p = sys.argv[1]
old, new = os.environ["OLD"], os.environ["NEW"]
lines = open(p).readlines()
hits = [i for i, l in enumerate(lines)
        if old in l and not l.lstrip().startswith(("#", '"', "'"))]
if not hits:
    sys.exit(3 if not any(old in l for l in lines) else 4)
if sum(l.count(old) for l in lines) != 1:
    sys.exit(5)
i = hits[0]
lines[i] = lines[i].replace(old, new, 1)
open(p, "w").writelines(lines)
PY
  rc=$?
  case "$rc" in
    0) ;;
    3) bad "$desc — mutation anchor not found; the gate would have scored a phantom kill"; return ;;
    4) bad "$desc — anchor exists only in comments/docstrings; mutating it proves nothing"; return ;;
    5) bad "$desc — anchor is not unique; the mutant would be ambiguous"; return ;;
    *) bad "$desc — mutation step failed (rc=$rc)"; return ;;
  esac
  if cmp -s "$SRC" "$dir/balancer_silence_guard.py"; then
    bad "$desc — mutation did not change the file"
    return
  fi
  if (cd "$dir" && python3 -m unittest test_balancer_silence_guard.py) >/dev/null 2>&1; then
    bad "$desc — SURVIVED: the suite passed against a broken guard"
  else
    ok "$desc"
  fi
}

hdr "=== each limb of the guard, broken one at a time ==="

mutate "freshness threshold softened to >= (stale-at-threshold scores fresh)" \
  'if age_min > args.max_age_min:' \
  'if age_min >= args.max_age_min:'

mutate "gap threshold softened to >= (a threshold-long gap goes unreported)" \
  'if delta_s > threshold_s and right[0] >= window_start:' \
  'if delta_s >= threshold_s and right[0] >= window_start:'

mutate "stale verdict reported as fresh (the 193-min silence scores green)" \
  '        print(f"STALE: balancer quiet for {age_min:.1f} min")' \
  '        print(f"FRESH: balancer quiet for {age_min:.1f} min")'

mutate "stale exit code flattened to 0 (callers never see the alarm)" \
  '        return 1' \
  '        return 0'

mutate "unmeasurable refusal unhandled (missing log ends in a traceback)" \
  '    except Unmeasurable as exc:' \
  '    except ImpossibleError as exc:'

printf '\npassed %d, failed %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
