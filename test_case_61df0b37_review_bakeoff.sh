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

# Only the historical private evaluation bundle is absent from a fresh public
# snapshot: curated reviewer labels and raw model-response results. A sanitized
# report may remain, but cannot be re-scored without those private inputs.
# The harness unit/mutation tests above always run. A partial raw-data bundle or
# missing artifacts in any nonpublic checkout still fails the assertions below.
if [[ -f .github/scripts/public_safety.py ]] &&
   grep -qFx '## Fresh-history publication candidate' README.md &&
   [[ ! -e "$E/dataset" && ! -e "$E/results" ]]; then
  printf '%s\n' 'SKIP: historical private bake-off dataset and raw responses are excluded from the fresh public snapshot; the retained report cannot be re-scored here; harness tests remain enabled.'
  exit "$FAIL"
fi

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


def split_row(line):
    return [c.strip() for c in line.strip("|").split("|")]


def is_separator(line):
    return all(re.fullmatch(r":?--+?:?", c.strip()) for c in split_row(line))


def score_table(stdout):
    # The scorer prints one Markdown table (header + separator + model rows)
    # followed by a comparisons footer; select table rows by parsed headers so
    # added columns and the footer cannot shift or masquerade as cells.
    lines = [ln for ln in stdout.splitlines() if ln.startswith("|")]
    assert len(lines) >= 3, "no score table in scorer output: %r" % stdout[:200]
    header = [c.strip().lower() for c in split_row(lines[0])]
    assert is_separator(lines[1]), "second table line is not the separator: %r" % lines[1]

    def col(*needles):
        for i, name in enumerate(header):
            if all(n in name for n in needles):
                return i
        raise AssertionError("no column %r in header %r" % (needles, header))

    mi = col("model")
    ri = col("recall", "found/defects")
    fi = col("false-pass")
    found = {}
    for ln in lines[2:]:
        if is_separator(ln):
            continue
        cells = split_row(ln)
        assert len(cells) == len(header), (len(cells), len(header), ln[:120])
        found[cells[mi]] = cells
    return found, ri, fi


seen = set()
for arm in arms:
    out = subprocess.run(
        [sys.executable, base + "/score.py", "--cases", base + "/dataset/cases.json",
         "--results", base + "/results/%s-medium-r1" % arm],
        capture_output=True, text=True, check=True).stdout
    found, ri, fi = score_table(out)
    model = next((m for m in found if arm in m or m.startswith(arm)), None)
    assert model is not None, "no scorer row for arm " + arm
    recall, false_pass = found[model][ri], found[model][fi]
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
  bash -c "grep -rEq 'ghp_[A-Za-z0-9]{20}|github_pat_[A-Za-z0-9_]{20}|sk-[A-Za-z0-9]{20,}|BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY|Bearer [A-Za-z0-9._-]{20,}' $E/results $E/dataset; test \$? -eq 1"

exit $FAIL
