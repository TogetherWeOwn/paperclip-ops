#!/usr/bin/env bash
# case_96f9122f. Offline, hermetic suite for the reviewer-model bake-off under eval/review-bakeoff/:
#   1. the harness's own unit tests (szz miner, case builder, run harness, scorer, batch driver)
#   2. dataset shape: 19 cases = 11 real + 8 planted, every case labelled
#   3. committed results re-score to the numbers the round-1 table publishes, and a
#      mutated copy of the table is rejected (so the check cannot pass vacuously)
#   4. no credential-shaped string in any committed raw result
# No network, no credential, no model call. Asserts on values and exit status, not message text.
set -u
cd "$(dirname "$0")"
E=eval/review-bakeoff
FAIL=0
ok()   { printf 'ok   %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; FAIL=1; }
check() { local name=$1; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }
T=$(mktemp -d "${TMPDIR:-/tmp}/case_61df0b37.XXXXXX"); trap 'rm -rf "$T"' EXIT

# --- 1. harness unit tests ----------------------------------------------------------------
check "harness unit tests pass" python3 -m unittest discover -s "$E" -p 'test_*.py'

# --- 2. dataset shape ---------------------------------------------------------------------
python3 - "$E" <<'PY' && ok "dataset: 19 cases, 11 real + 8 planted, each with a defect list" || bad "dataset shape"
import json, sys
cases = json.load(open(sys.argv[1] + "/dataset/cases.json"))["cases"]
ids = [c["id"] for c in cases]
assert len(ids) == len(set(ids)) == 19, len(ids)
assert sum(i.startswith("r-") for i in ids) == 11
assert sum(i.startswith("p-") for i in ids) == 8
assert all(c.get("defects") for c in cases), [c["id"] for c in cases if not c.get("defects")]
PY

# --- 3. committed results re-score to the published table ---------------------------------
# Each arm directory maps to the served model id the scorer prints; the table row for that id
# must carry the scorer's recall and false-pass cells verbatim.
cat > "$T/check_table.py" <<'PY'
import re, subprocess, sys
base, table = sys.argv[1], sys.argv[2]
arms = ["opus", "sonnet", "astra", "sol", "muse", "glm", "codex"]
text = open(table, encoding="utf-8").read()
rows = {}
for line in text.splitlines():
    m = re.search(r"`([a-z0-9.\-]+)`\)", line)
    if line.startswith("|") and m:
        rows[m.group(1)] = [c.strip() for c in line.strip("|").split("|")]
seen = set()
for arm in arms:
    out = subprocess.run(
        [sys.executable, base + "/score.py", "--cases", base + "/dataset/cases.json",
         "--results", base + "/results/%s-medium-r1" % arm],
        capture_output=True, text=True, check=True).stdout.strip().splitlines()[-1]
    cells = [c.strip() for c in out.strip("|").split("|")]
    model, recall, false_pass = cells[0], cells[3], cells[10]
    assert model in rows, "no table row for " + model
    row = rows[model]
    assert recall in row, (model, recall, row)
    assert false_pass in row, (model, false_pass, row)
    seen.add(model)
assert len(seen) == 7, seen
PY
check "round-1 table equals the scorer's output for all 7 arms" \
  python3 "$T/check_table.py" "$E" "$E/RESULTS-r1.md"
sed 's#15/24 = 0.62#16/24 = 0.67#' "$E/RESULTS-r1.md" > "$T/mutated.md"
check "a table with one edited recall cell is rejected" \
  bash -c "! python3 '$T/check_table.py' '$E' '$T/mutated.md' 2>/dev/null"

# --- 4. no credential-shaped string in committed raw results ------------------------------
check "no token-shaped string in results/ or dataset/" \
  bash -c "! grep -rEq 'ghp_[A-Za-z0-9]{20}|github_pat_[A-Za-z0-9_]{20}|sk-[A-Za-z0-9]{20,}|BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY|Bearer [A-Za-z0-9._-]{20,}' $E/results $E/dataset"

exit $FAIL
