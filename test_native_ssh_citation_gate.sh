#!/usr/bin/env bash
# Mutation gate for native_ssh_citation_gate.py.
#
# A citation gate that cannot go red is decoration. Each case below breaks exactly ONE
# property in a fixture tree and asserts the gate reports that specific verdict with the
# specific exit code. Cases are independent: a mutation that trips two checks at once
# would let one broken check hide behind the other.
#
# Exit 0 = every case behaved as required.

set -euo pipefail

GATE="$(cd "$(dirname "$0")" && pwd)/native_ssh_citation_gate.py"
FIXROOT="$(mktemp -d)"
trap 'rm -rf "$FIXROOT"' EXIT

pass=0
fail=0

# ---------------------------------------------------------------------------
# Build a fixture tree that MIRRORS the installed tree exactly, by copying the
# real files. Copying (not synthesizing) means the baseline is green for the same
# reason the real run is green — a hand-written fixture could pass while the real
# tree failed.
# ---------------------------------------------------------------------------
build_fixture() {
  local dest="$1"
  rm -rf "$dest"
  for f in \
    /app/packages/adapter-utils/src/server-utils.ts \
    /app/packages/adapter-utils/src/ssh.ts \
    /app/packages/adapter-utils/src/remote-managed-runtime.ts \
    /app/packages/adapter-utils/src/acpx-engine/execute.ts \
    /app/server/src/services/heartbeat.ts \
    /app/server/src/adapters/plugin-loader.ts \
    /app/server/src/services/decision-signing.ts ; do
    mkdir -p "$dest$(dirname "$f")"
    cp "$f" "$dest$f"
  done
}

# run_case <name> <expected_exit> <expected_substring> [mutator...]
run_case() {
  local name="$1" want_exit="$2" want_text="$3"; shift 3
  local dir="$FIXROOT/$name"
  build_fixture "$dir"
  if [ "$#" -gt 0 ]; then "$@" "$dir"; fi

  local out rc
  set +e
  out="$(python3 "$GATE" --root "$dir" 2>&1)"
  rc=$?
  set -e

  if [ "$rc" -eq "$want_exit" ] && grep -qF "$want_text" <<<"$out"; then
    echo "  [PASS] $name (exit=$rc)"
    pass=$((pass + 1))
  else
    echo "  [FAIL] $name: wanted exit=$want_exit and text '$want_text', got exit=$rc"
    printf '%s\n' "$out" | sed 's/^/         | /'
    fail=$((fail + 1))
  fi
}

# --- mutators ---------------------------------------------------------------

# Insert a line ABOVE the anchor so the anchor shifts down by one: pure drift, the
# anchor still exists. This is the silent-rot case the gate exists to catch.
mut_drift() {
  local d="$1"
  local f="$d/app/packages/adapter-utils/src/server-utils.ts"
  printf '// drift\n%s' "$(cat "$f")" > "$f.tmp" && mv "$f.tmp" "$f"
}

# Delete the anchor text entirely: the seam is gone / was never deployed.
mut_absent() {
  local d="$1"
  local f="$d/app/server/src/services/heartbeat.ts"
  sed -i 's/adapterResult = await adapter\.execute(/adapterResult = await REMOVED_SEAM(/' "$f"
}

# Remove the whole file: absent must not be reported as "present" or crash.
mut_missing_file() {
  local d="$1"
  rm -f "$d/app/server/src/services/decision-signing.ts"
}

# Make an anchor non-unique. A first-match resolver would silently answer with the
# WRONG site; the gate must refuse (exit 2) rather than pick one.
mut_ambiguous() {
  local d="$1"
  local f="$d/app/packages/adapter-utils/src/ssh.ts"
  printf '\nexport async function buildSshSpawnTarget(/*dup*/) {}\n' >> "$f"
}

# Introduce a symbol the ADR asserts is NOT deployed. If case_e4054a66's seam ever lands,
# the "not deployed" finding is stale and the gate must fail loudly.
mut_seam_appears() {
  local d="$1"
  local f="$d/app/packages/adapter-utils/src/server-utils.ts"
  printf '\nconst hostExecutor = null;\n' >> "$f"
}

# --- cases ------------------------------------------------------------------

echo "Mutation gate: native_ssh_citation_gate.py"
echo

# Positive control FIRST. If the unmutated fixture is not green, every red below is
# meaningless — it would prove only that the fixture is broken.
run_case "control_unmutated_is_green" 0 "failures=0 audit_errors=0"

run_case "drift_is_detected"        1 "DRIFT" mut_drift
run_case "absent_anchor_detected"   1 "ABSENT" mut_absent
run_case "missing_file_detected"    1 "file_not_found" mut_missing_file
run_case "ambiguous_anchor_refused" 2 "anchor_not_unique" mut_ambiguous
run_case "undeployed_seam_appearing_fails" 1 "hostExecutor: 1 occurrence" mut_seam_appears

# Negative control: a mutation the gate should IGNORE. Editing an unrelated region
# must stay green, otherwise the gate is just detecting "file changed" and every red
# above is uninformative.
mut_irrelevant() {
  local d="$1"
  local f="$d/app/packages/adapter-utils/src/ssh.ts"
  printf '\n// unrelated trailing comment\n' >> "$f"
}
run_case "unrelated_edit_stays_green" 0 "failures=0 audit_errors=0" mut_irrelevant

echo
echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
