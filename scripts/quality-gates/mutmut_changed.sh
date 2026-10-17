#!/usr/bin/env bash
# Mutmut-on-changed-lines gate (WARN): mutation testing on changed lines only.
#
# Diffs the PR against its base SHA and hands that patch to mutmut 2.x via
# --use-patch-file, so only added/changed lines are mutated. The test runner
# is scripts/verify-fast.sh (the repo's fast tier), and a hard
# wall-clock budget bounds the whole mutmut invocation inside the job's
# timeout. Survived mutants print as ::warning:: annotations. Mutants are
# scoped with --paths-to-mutate built from the changed non-test .py files
# (mutmut guesses lib/ and checks 0 mutants without it); survivors are read
# from `mutmut results` / `mutmut result-ids survived`, not from a log grep
# that only ever matches the legend line. mutmut applies each mutant to the
# tracked file in place, so the run happens in a throwaway worktree pinned at
# HEAD -- never in this checkout. A budget kill (SIGTERM mid-mutant, rc 124)
# would otherwise leave a mutated file behind for the later HARD gate 8 to
# read; tearing the worktree down drops the in-flight mutant and
# .mutmut-cache together.
#
# Exit is ALWAYS 0: WARN mode, never fails -- including when mutmut finds
# nothing, the budget expires, or there are no changed .py lines. A budget
# that evaluates zero mutants still warns (vacuity signal) rather than
# passing silently.
#
# Env:
#   BASE_SHA            PR base SHA (required on pull_request; skipped otherwise)
#   MUTMUT_BUDGET_SECS  wall-clock budget for `mutmut run` (default 240)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

TMP_DIR="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
BUDGET="${MUTMUT_BUDGET_SECS:-240}"

if [[ "${GITHUB_EVENT_NAME:-}" != "pull_request" && -z "${BASE_SHA:-}" ]]; then
  echo "mutation gate: non-PR event without BASE_SHA, nothing to diff -- skipping"
  exit 0
fi

base="${BASE_SHA:-${GITHUB_EVENT_PULL_REQUEST_BASE_SHA:-}}"
if [[ -z "$base" ]]; then
  echo "::warning::mutation gate: no base SHA available, skipping (not failing)"
  exit 0
fi

# Full history is already present (fetch-depth 0); the base must resolve
# locally. Fail loudly when it does not instead of fetching.
if ! git cat-file -e "$base^{commit}" 2>/dev/null; then
  echo "::warning::mutation gate: base $base not in clone, skipping (not failing)"
  exit 0
fi

patch_file="$TMP_DIR/mutmut-changed.patch"
mutmut_log="$TMP_DIR/mutmut.log"
git diff --no-renames "$base" HEAD -- '*.py' >"$patch_file" || true
if [[ ! -s "$patch_file" ]]; then
  echo "mutation gate: no changed .py lines -- skipping"
  exit 0
fi
echo "mutation gate: changed .py diff: $(wc -l <"$patch_file") lines"

# Scope mutants to the changed non-test .py files, comma-separated for
# mutmut's --paths-to-mutate (splits on commas/colons). Test files are
# excluded: mutating tests measures nothing.
mutate_paths="$(git diff --no-renames --name-only "$base" HEAD -- '*.py' \
  | python3 -c 'import sys
from pathlib import Path
out = []
for line in sys.stdin:
    p = line.strip()
    if not p:
        continue
    parts = Path(p).parts
    name = parts[-1] if parts else p
    if name.startswith("test_") or name.endswith("_test.py"):
        continue
    if any(d in ("test", "tests") for d in parts):
        continue
    out.append(p)
print(",".join(out))')"
if [[ -z "$mutate_paths" ]]; then
  echo "::warning::mutation gate: only test .py files changed -- nothing to mutate (vacuity signal, not failing)"
  exit 0
fi
echo "mutation gate: paths to mutate: $mutate_paths"

# mutmut 2.x CLI, pinned in the workflow (mutmut[patch] extra for
# --use-patch-file's whatthepatch import). --use-patch-file restricts
# mutation to added/changed lines; --runner is the repo fast tier per the
# card; --CI keeps mutmut's own exit code out of the way (this gate reports
# via annotations and always exits 0); the outer `timeout` enforces the
# budget (124 = budget expired, a WARN here, not a failure). Everything below
# runs with cwd inside the throwaway worktree: the run, the results reads,
# and mutmut's own .mutmut-cache all land there, so this checkout is never
# mutated -- even when the budget SIGTERMs mutmut mid-mutant.
mut_wt="$TMP_DIR/mutmut-wt-$$"
if ! git worktree add --detach "$mut_wt" HEAD >/dev/null 2>&1; then
  echo "::warning::mutation gate: cannot create throwaway worktree, skipping (not failing)"
  exit 0
fi
trap 'git worktree remove --force "$mut_wt" >/dev/null 2>&1 || true' EXIT
set +e
(
  cd "$mut_wt" && timeout "$BUDGET" python3 -m mutmut run \
    --use-patch-file "$patch_file" \
    --paths-to-mutate "$mutate_paths" \
    --runner './scripts/verify-fast.sh' \
    --simple-output --no-progress --CI >"$mutmut_log" 2>&1
)
rc=$?
set -e

if [[ $rc -eq 124 ]]; then
  echo "::warning::mutation gate: budget (${BUDGET}s) expired before mutants finished"
elif [[ $rc -ne 0 && $rc -ne 2 && $rc -ne 4 && $rc -ne 8 ]]; then
  echo "::warning::mutation gate: mutmut exited $rc; log tail:"
  tail -n 20 "$mutmut_log" || true
  exit 0
fi

# Survivors come from `mutmut results`, not from grepping the run log (the
# log only ever contains the SURVIVED legend line). `result-ids survived`
# prints the surviving mutant ids separated by spaces. Read from inside the
# throwaway worktree, where the run wrote its .mutmut-cache.
survived_ids="$(cd "$mut_wt" && python3 -m mutmut result-ids survived 2>/dev/null || true)"
survived_ids="$(printf '%s' "$survived_ids" | tr -s '[:space:]' '\n' | grep -c . || true)"
echo "mutation gate: survived~${survived_ids} (see mutmut results below)"
(cd "$mut_wt" && python3 -m mutmut results 2>&1 | head -n 30) || true
if [[ "${survived_ids:-0}" != "0" ]]; then
  (cd "$mut_wt" && python3 -m mutmut result-ids survived 2>/dev/null) \
    | tr -s '[:space:]' '\n' | head -n 20 | while IFS= read -r mid; do
    [[ -n "$mid" ]] || continue
    echo "::warning::mutation survived: $mid"
  done || true
else
  echo "mutation gate: no survivors above (clean or vacuity -- see results)"
fi
exit 0
