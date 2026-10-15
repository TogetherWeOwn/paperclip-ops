#!/usr/bin/env bash
# ===========================================================================
# test_readme_script_index.sh — gate for the README "What is here" script
# index (case_d464f0a6).
#
# THE GAP THIS EXISTS FOR. 35 of the 60 tracked root scripts had no README
# row (`pacer_apply.py`, `runner_disk_pressure.sh`, `dlq_redrive.sh`,
# `host_db_backup.sh`, `db_idle_transaction_stall.js`, ...), and
# `queue_liveness.sh` / `quota_brake.sh` each appeared twice. An operator
# meeting a red alarm could not find the right script from the repo alone.
#
# THE LOAD-BEARING PROPERTIES, in the order they matter:
#
#   1. It FAILS on the real gap: every tracked, operator-facing root script
#      without an index row is named, and the run exits non-zero.
#   2. It FAILS on duplicates: a script with two rows is a finding, so the
#      case_d464f0a6 duplicates cannot come back.
#   3. It is NOT vacuous: a fixture with (indexed script, exempted helper,
#      missing script) passes the first two and fails the third BY NAME, and
#      a copy of the real README minus one row fails too — the mutation
#      control proving this gate can go red.
#   4. Exemptions are explicit, never silent: non-operator helpers live in
#      EXEMPT below with a reason each, are named in the README note, and a
#      stale exemption (naming nothing tracked) fails.
#
# Fully offline: git ls-files (local read), mktemp fixtures, no network, no
# credential, no database.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE"
README_FILE="${README_FILE:-$ROOT/README.md}"

# Non-operator root helpers with no index row. Short by design: everything an
# operator could reach for on an alarm stays indexed. Each entry names a
# tracked root script and carries its reason here AND in the README note.
EXEMPT=(
  # One-shot case_6684d547 forensic export: parses an operator-supplied collector
  # copy and emits a redacted seam. Not a standing monitor, gate, or runbook
  # step — nothing pages anybody to run it.
  "cliproxy_seam_extract.py"
)
# Fixture sections override the exempt set through README_INDEX_EXEMPT so the
# fixture proves the exemption path without touching the real list. The
# override is resolved inside check_index (not once here), so an inline
# `README_INDEX_EXEMPT=... check_index` assignment takes effect.

PASS=0; FAIL=0
ok() { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1"; [[ -n "${2:-}" ]] && printf '       %s\n' "$2"; }
section() { printf '\n== %s\n' "$1"; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test_readme_script_index.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# Basenames of every backticked *.sh/*.py/*.js/*.mjs in the FIRST (Tool) cell
# of the "What is here" table. Scoped to that section so code blocks elsewhere in
# the README cannot satisfy the gate; Purpose and Suite cells are ignored so
# a suite or a mentioned tool never reads as an index row. Paths reduce to
# basenames so `ops/...` and `lib/...` rows parse.
indexed_names() { # $1=readme
  awk '/^## What is here/{flag=1;next} /^## /{flag=0} flag && /^\|/' "$1" \
    | cut -d'|' -f2 \
    | grep -o '`[^`]*\.\(sh\|py\|js\|mjs\)`' \
    | tr -d '`' \
    | sed 's#.*/##' \
    | sort
}

# Tracked root scripts: git-committed *.sh/*.py/*.js/*.mjs with no slash,
# minus test_*. Vendored restores are git-ignored, so they never appear here;
# their rows stay informational and this gate neither requires nor forbids them.
tracked_names() {
  git -C "$ROOT" ls-files '*.sh' '*.py' '*.js' '*.mjs' \
    | grep -v '/' \
    | grep -v '^test_' \
    | sort -u
}

# check_index <readme> <tracked-file>: prints MISSING/DUPLICATE lines naming
# the script. Exit 0 clean, 1 findings. Reads README_INDEX_EXEMPT from the
# env when set — even when set empty, which means zero exemptions — else the
# EXEMPT list above. (`-`, not `:-`: an explicit empty override must not
# silently resolve back to the real list.)
check_index() {
  local readme="$1" tracked="$2" rc=0
  local exempt_src="${README_INDEX_EXEMPT-${EXEMPT[*]}}"
  [[ -f "$readme" ]] || { echo "MISSING-README: $readme"; return 1; }
  indexed_names "$readme" > "$WORK/indexed.txt"
  sort -u "$WORK/indexed.txt" > "$WORK/indexed_u.txt"
  # Duplicates first: a doubled row is a finding even when coverage holds.
  local dups
  dups="$(uniq -d "$WORK/indexed.txt" || true)"
  if [[ -n "$dups" ]]; then
    while IFS= read -r d; do echo "DUPLICATE: $d"; done <<< "$dups"
    rc=1
  fi
  # shellcheck disable=SC2206
  local exempt_list=($exempt_src)
  local wanted="$WORK/wanted.txt"
  : > "$wanted"
  while IFS= read -r t; do
    local skip=0
    for e in ${exempt_list[@]+"${exempt_list[@]}"}; do
      [[ "$t" == "$e" ]] && { skip=1; break; }
    done
    [[ "$skip" -eq 0 ]] && echo "$t" >> "$wanted"
  done < "$tracked"
  local missing
  missing="$(comm -23 "$wanted" "$WORK/indexed_u.txt" || true)"
  if [[ -n "$missing" ]]; then
    while IFS= read -r m; do echo "MISSING: $m"; done <<< "$missing"
    rc=1
  fi
  return "$rc"
}

# --- 1. the live repo -------------------------------------------------------
section "Live README covers every tracked operator-facing root script"
tracked_names > "$WORK/tracked.txt"
if out="$(check_index "$README_FILE" "$WORK/tracked.txt" 2>&1)"; then
  ok "no missing rows, no duplicates"
else
  bad "index drift" "$out"
fi
# Stale exemptions fail: an EXEMPT entry that names nothing tracked is a
# silent hole, not a short list.
stale=0
for e in "${EXEMPT[@]}"; do
  grep -qxF "$e" "$WORK/tracked.txt" || { bad "stale exemption names nothing tracked" "$e"; stale=1; }
done
[[ "$stale" -eq 0 ]] && ok "every exemption names a tracked script"

# --- 2. fixture: indexed passes, exempted passes, missing fails by name -----
section "Fixture: indexed and exempted pass, missing fails naming the script"
cat > "$WORK/fixture-readme.md" <<'EOF'
## What is here

| Tool | Purpose | Suite |
|---|---|---|
| `have_me.sh` | Has a row. | `test_have_me.sh` |
| `also_have.py` | Has a row. | — |

## Something else

`| not_a_table.sh |` code ticks outside the section must not count.

| `out_of_section.sh` | A valid-looking row, but outside the indexed section. | — |
EOF
printf '%s\n' "have_me.sh" "also_have.py" "exempted_helper.py" "ghost_missing.sh" "out_of_section.sh" | sort > "$WORK/fixture-tracked.txt"
if out="$(README_INDEX_EXEMPT="exempted_helper.py" check_index "$WORK/fixture-readme.md" "$WORK/fixture-tracked.txt" 2>&1)"; then
  bad "fixture missing script did not fail" "$out"
else
  if grep -q "MISSING: ghost_missing.sh" <<< "$out" \
    && grep -q "MISSING: out_of_section.sh" <<< "$out" \
    && ! grep -q "MISSING: have_me.sh" <<< "$out" \
    && ! grep -q "MISSING: also_have.py" <<< "$out" \
    && ! grep -q "MISSING: exempted_helper.py" <<< "$out"; then
    ok "missing scripts fail by name; indexed and exempted rows stay quiet; out-of-section rows do not count"
  else
    bad "fixture failure names the wrong set" "$out"
  fi
fi

# --- 3. fixture: a doubled row fails naming it --------------------------------
section "Fixture: duplicate rows fail naming the script"
cat > "$WORK/dup-readme.md" <<'EOF'
## What is here

| Tool | Purpose | Suite |
|---|---|---|
| `have_me.sh` | First row. | `test_have_me.sh` |
| `have_me.sh` | Second row. | `test_have_me.sh` |

## End
EOF
printf '%s\n' "have_me.sh" | sort > "$WORK/dup-tracked.txt"
if out="$(README_INDEX_EXEMPT="" check_index "$WORK/dup-readme.md" "$WORK/dup-tracked.txt" 2>&1)"; then
  bad "duplicate rows did not fail" "$out"
else
  if grep -q "DUPLICATE: have_me.sh" <<< "$out"; then
    ok "duplicate fails by name"
  else
    bad "duplicate failure names the wrong script" "$out"
  fi
fi

# --- 4. mutation: dropping one real row must turn the gate red ----------------
section "Mutation: the real README minus one row fails (gate is not vacuous)"
if ! grep -q '^\s*| `runner_disk_pressure.sh`' "$README_FILE"; then
  bad "mutation anchor absent: runner_disk_pressure.sh has no row to drop"
else
  grep -v '^\s*| `runner_disk_pressure.sh`' "$README_FILE" > "$WORK/mutated-readme.md"
  if out="$(check_index "$WORK/mutated-readme.md" "$WORK/tracked.txt" 2>&1)"; then
    bad "mutated README (row dropped) stayed green"
  else
    if grep -q "MISSING: runner_disk_pressure.sh" <<< "$out"; then
      ok "dropped row fails by name"
    else
      bad "mutation failure names the wrong script" "$out"
    fi
  fi
fi
# Baseline for the attribution above: the unmutated README passes the same
# checker in the same directory, so "the mutant failed" means the checker saw
# the row go missing, not that the checker always fails.
if check_index "$README_FILE" "$WORK/tracked.txt" >/dev/null 2>&1; then
  ok "unmutated README passes the same checker"
else
  bad "unmutated README fails its own gate"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
