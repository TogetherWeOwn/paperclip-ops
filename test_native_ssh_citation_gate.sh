#!/usr/bin/env bash
# Mutation gate for native_ssh_citation_gate.py.
#
# A citation gate that cannot go red is decoration. Each case below breaks exactly ONE
# property in a fixture tree and asserts the gate reports that specific verdict with the
# specific exit code. Cases are independent: a mutation that trips two checks at once
# would let one broken check hide behind the other.
#
# The fixtures are SYNTHESIZED at the pinned lines, not copied from a live tree, so
# this suite runs hermetically in public CI (no /app there). What the suite proves is
# the gate's logic — present/drifted/absent/ambiguous verdicts and the
# expected-present seam contract — around the recorded pins. That the pins themselves
# match the live tree is established out of band by running the gate against the
# deployed tree and recording the commit in docs/native-ssh-os-authority-split.md.
#
# Exit 0 = every case behaved as required.

set -euo pipefail

GATE="$(cd "$(dirname "$0")" && pwd)/native_ssh_citation_gate.py"
FIXROOT="$(mktemp -d)"
trap 'rm -rf "$FIXROOT"' EXIT

pass=0
fail=0

# ---------------------------------------------------------------------------
# Build a fixture tree with every citation anchor at exactly its recorded line
# and the three deployed seam symbols present in server-utils.ts (zero in
# ssh.ts, mirroring the live tree). Filler lines are inert: they contain no
# anchor and no seam symbol, so each anchor is unique within its file.
# ---------------------------------------------------------------------------
build_fixture() {
  local dest="$1"
  rm -rf "$dest"
  python3 - "$dest" <<'EOF'
import sys
from pathlib import Path

dest = Path(sys.argv[1])

# path -> (total_lines, {line_number: content})
FILES = {
    "app/packages/adapter-utils/src/server-utils.ts": (4950, {
        3741: "async function resolveSpawnTarget(target: SpawnTarget) {",
        3787: '    const sshResolved = await resolveCommandPath("ssh", process.cwd(), env);',
        4833: "export async function runChildProcess(cmd: string) {",
        4934: "        const child = spawn(target.command, target.args, {",
        # Deployed seam symbols: present here, absent from ssh.ts (as live).
        # None of these lines may contain a citation anchor, and no anchor
        # line may contain a seam symbol — the seam-absent mutation relies on
        # both directions to fail exactly on the seam lines.
        100: "const hostExecutor = null; // seam handle",
        101: "type HostExecutor = typeof hostExecutor;",
        102: "await authorizeLaunch(envelope);",
    }),
    "app/packages/adapter-utils/src/ssh.ts": (1560, {
        1541: "export async function buildSshSpawnTarget(input: SshInput) {",
    }),
    "app/packages/adapter-utils/src/remote-managed-runtime.ts": (150, {
        138: '  const runtimeRootDir = path.posix.join(workspaceRemoteDir, ".paperclip-runtime", input.adapterKey);',
    }),
    "app/packages/adapter-utils/src/acpx-engine/execute.ts": (4270, {
        4256: "        const runtimeOptions: PaperclipAcpRuntimeOptions = {",
    }),
    "app/server/src/services/heartbeat.ts": (25900, {
        25879: "                  () => adapter.execute({",
    }),
    "app/server/src/adapters/plugin-loader.ts": (210, {
        195: "  const mod = await import(pathToFileURL(modulePath).href);",
    }),
    "app/server/src/services/decision-signing.ts": (30, {
        17: "  if (currentUserId !== undefined && stats.uid !== currentUserId) {",
    }),
}

for rel, (total, placed) in FILES.items():
    path = dest / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for n in range(1, total + 1):
            fh.write(placed.get(n, f"// filler {rel} line {n}") + "\n")
EOF
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
  sed -i 's/() => adapter\.execute({/() => REMOVED_SEAM({/' "$f"
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

# Rewrite every deployed seam symbol in place with a same-length run of X: the seam
# is gone from a tree where it must be PRESENT. In place, not whole-line deletes —
# deleting lines would shift every later anchor and bury the seam verdict under
# DRIFT noise. The gate must fail loudly with a per-symbol [FAIL] line.
mut_seam_absent() {
  local d="$1"
  local f="$d/app/packages/adapter-utils/src/server-utils.ts"
  sed -i -e 's/hostExecutor/XXXXXXXXXXXX/g' \
         -e 's/HostExecutor/XXXXXXXXXXXX/g' \
         -e 's/authorizeLaunch/XXXXXXXXXXXXXXX/g' "$f"
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

# Expected-present contract (case_e4054a66 seam deployed): the gate MUST emit
# header "Symbols required PRESENT (case_e4054a66 seam deployed):" and per-symbol
# lines "  [FAIL] {symbol}: 0 occurrence(s)" when absent. The mutated run must
# fail EXACTLY on the three seam lines: failures=3 with no DRIFT and no ABSENT,
# proving the in-place mutation shifted no anchor.
run_seam_absent_case() {
  local name="deployed_seam_absent_fails"
  local dir="$FIXROOT/$name"
  build_fixture "$dir"
  mut_seam_absent "$dir"

  local out rc
  set +e
  out="$(python3 "$GATE" --root "$dir" 2>&1)"
  rc=$?
  set -e

  local ok=1
  [ "$rc" -eq 1 ] || ok=0
  grep -qF "[FAIL] hostExecutor: 0 occurrence(s)" <<<"$out" || ok=0
  grep -qF "[FAIL] HostExecutor: 0 occurrence(s)" <<<"$out" || ok=0
  grep -qF "[FAIL] authorizeLaunch: 0 occurrence(s)" <<<"$out" || ok=0
  grep -qF "failures=3 audit_errors=0" <<<"$out" || ok=0
  grep -q "DRIFT" <<<"$out" && ok=0
  grep -q "ABSENT" <<<"$out" && ok=0

  if [ "$ok" -eq 1 ]; then
    echo "  [PASS] $name (exit=$rc, exactly the 3 seam lines)"
    pass=$((pass + 1))
  else
    echo "  [FAIL] $name: wanted exit=1 with exactly the 3 seam [FAIL] lines, failures=3, no DRIFT/ABSENT; got exit=$rc"
    printf '%s\n' "$out" | sed 's/^/         | /'
    fail=$((fail + 1))
  fi
}
run_seam_absent_case

# Forbidden-file contract: a seam symbol added to ssh.ts must fail even though the
# required file still holds all three symbols. Without this case a regression that
# stops scanning ssh.ts would stay green. Appended at EOF so no anchor shifts.
mut_seam_in_forbidden_file() {
  local d="$1"
  local f="$d/app/packages/adapter-utils/src/ssh.ts"
  printf '\nconst hostExecutor = null;\n' >> "$f"
}
run_case "seam_in_forbidden_file_fails" 1 "unexpectedly_present_in_ssh" mut_seam_in_forbidden_file

# --- lexical counting contract -------------------------------------------
# The required-file count must exclude comments, strings, regex/template
# literals and identifier substrings (counting any of those as deployed code
# would be a false present), while the forbidden-file count must still catch
# executable seam references in division chains and template interpolations
# (missing those would be a false clean in ssh.ts). Each mutator below rewrites
# the three seam lines (100-102) IN PLACE so no anchor shifts.

mut_lexical_comment_only() {
  local d="$1"
  python3 - "$d/app/packages/adapter-utils/src/server-utils.ts" <<'EOF'
import sys
p = sys.argv[1]
lines = open(p, encoding="utf-8").readlines()
lines[99] = "// const hostExecutor = null;\n"
lines[100] = "// type HostExecutor = typeof hostExecutor;\n"
lines[101] = "// await authorizeLaunch(envelope);\n"
open(p, "w", encoding="utf-8").writelines(lines)
EOF
}

mut_lexical_string_only() {
  local d="$1"
  python3 - "$d/app/packages/adapter-utils/src/server-utils.ts" <<'EOF'
import sys
p = sys.argv[1]
lines = open(p, encoding="utf-8").readlines()
lines[99] = 'const a = "hostExecutor";\n'
lines[100] = 'const b = "HostExecutor hostExecutor";\n'
lines[101] = "const c = 'authorizeLaunch';\n"
open(p, "w", encoding="utf-8").writelines(lines)
EOF
}

mut_lexical_regex_only() {
  local d="$1"
  python3 - "$d/app/packages/adapter-utils/src/server-utils.ts" <<'EOF'
import sys
p = sys.argv[1]
lines = open(p, encoding="utf-8").readlines()
lines[99] = "const ra = /hostExecutor/;\n"
lines[100] = "const rb = /HostExecutor|hostExecutor/g;\n"
lines[101] = "if (/authorizeLaunch/.test(x)) {}\n"
open(p, "w", encoding="utf-8").writelines(lines)
EOF
}

mut_lexical_substring_only() {
  local d="$1"
  python3 - "$d/app/packages/adapter-utils/src/server-utils.ts" <<'EOF'
import sys
p = sys.argv[1]
lines = open(p, encoding="utf-8").readlines()
lines[99] = "const myhostExecutor = 1;\n"
lines[100] = "const HostExecutorish = myhostExecutor2;\n"
lines[101] = "const preauthorizeLaunchpost = 1;\n"
open(p, "w", encoding="utf-8").writelines(lines)
EOF
}

mut_lexical_template_text_only() {
  local d="$1"
  python3 - "$d/app/packages/adapter-utils/src/server-utils.ts" <<'EOF'
import sys
p = sys.argv[1]
lines = open(p, encoding="utf-8").readlines()
lines[99] = "const ta = `hostExecutor`;\n"
lines[100] = "const tb = `HostExecutor and ${42} hostExecutor`;\n"
lines[101] = "const tc = `authorizeLaunch`;\n"
open(p, "w", encoding="utf-8").writelines(lines)
EOF
}

# A lexical trap must read ABSENT: all three symbols at 0 occurrences with
# failures=3 and no DRIFT/ABSENT, exactly like the seam-absent case.
run_lexical_absent_case() {
  local name="$1"; shift
  local dir="$FIXROOT/$name"
  build_fixture "$dir"
  "$@" "$dir"

  local out rc
  set +e
  out="$(python3 "$GATE" --root "$dir" 2>&1)"
  rc=$?
  set -e

  local ok=1
  [ "$rc" -eq 1 ] || ok=0
  grep -qF "[FAIL] hostExecutor: 0 occurrence(s)" <<<"$out" || ok=0
  grep -qF "[FAIL] HostExecutor: 0 occurrence(s)" <<<"$out" || ok=0
  grep -qF "[FAIL] authorizeLaunch: 0 occurrence(s)" <<<"$out" || ok=0
  grep -qF "failures=3 audit_errors=0" <<<"$out" || ok=0
  grep -q "DRIFT" <<<"$out" && ok=0
  grep -q "ABSENT" <<<"$out" && ok=0

  if [ "$ok" -eq 1 ]; then
    echo "  [PASS] $name (exit=$rc, lexical trap reads absent)"
    pass=$((pass + 1))
  else
    echo "  [FAIL] $name: wanted exit=1 with all 3 symbols at 0, failures=3, no DRIFT/ABSENT; got exit=$rc"
    printf '%s\n' "$out" | sed 's/^/         | /'
    fail=$((fail + 1))
  fi
}

run_lexical_absent_case "lexical_comment_only_absent" mut_lexical_comment_only
run_lexical_absent_case "lexical_string_only_absent" mut_lexical_string_only
run_lexical_absent_case "lexical_regex_only_absent" mut_lexical_regex_only
run_lexical_absent_case "lexical_substring_only_absent" mut_lexical_substring_only
run_lexical_absent_case "lexical_template_text_only_absent" mut_lexical_template_text_only

# Deployed-name mapping: the live tree has no standalone HostExecutor; the
# label is satisfied only through NativeSshHostExecutorTarget and
# getActiveNativeSshHostExecutor. Drop the literal and require exactly those
# two to keep the gate green, or the mapping can break with CI still green.
mut_deployed_names_only() {
  local d="$1"
  python3 - "$d/app/packages/adapter-utils/src/server-utils.ts" <<'EOF'
import sys
p = sys.argv[1]
lines = open(p, encoding="utf-8").readlines()
lines[100] = "type T = NativeSshHostExecutorTarget; getActiveNativeSshHostExecutor();\n"
open(p, "w", encoding="utf-8").writelines(lines)
EOF
}

run_deployed_names_case() {
  local name="deployed_hostexecutor_names_satisfy_label"
  local dir="$FIXROOT/$name"
  build_fixture "$dir"
  mut_deployed_names_only "$dir"

  local json rc
  set +e
  json="$(python3 "$GATE" --root "$dir" --json 2>&1)"
  rc=$?
  set -e

  local ok=1
  [ "$rc" -eq 0 ] || ok=0
  python3 - "$json" <<'EOF' || ok=0
import json, sys
d = json.loads(sys.argv[1])
assert d["failures"] == 0, d
assert d["audit_errors"] == 0, d
ep = {e["symbol"]: e for e in d["expected_present"]}
assert ep["hostExecutor"]["occurrences"] == 1, ep
assert ep["HostExecutor"]["occurrences"] == 2, ep
assert ep["authorizeLaunch"]["occurrences"] == 1, ep
assert all(e["verdict"] == "present_as_expected" for e in ep.values()), ep
EOF

  if [ "$ok" -eq 1 ]; then
    echo "  [PASS] $name (exit=$rc, HostExecutor reads exactly the 2 deployed names)"
    pass=$((pass + 1))
  else
    echo "  [FAIL] $name: wanted exit=0 with HostExecutor occurrences=2; got exit=$rc"
    printf '%s\n' "$json" | sed 's/^/         | /'
    fail=$((fail + 1))
  fi
}
run_deployed_names_case

# Division and interpolation forms must be COUNTED in both files. The plain
# seam lines are X-ed out first so the verdict rests solely on the appended
# forms (at EOF, past every anchor, so no drift). Exact --json counts prove
# each individual form is seen: a lexer that swallows any one form undercounts.
mut_division_in_required() {
  local d="$1"
  local f="$d/app/packages/adapter-utils/src/server-utils.ts"
  sed -i -e 's/hostExecutor/XXXXXXXXXXXX/g' \
         -e 's/HostExecutor/XXXXXXXXXXXX/g' \
         -e 's/authorizeLaunch/XXXXXXXXXXXXXXX/g' "$f"
  python3 - "$f" <<'EOF'
import sys
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write("const r = total / hostExecutor / 2;\n")
    fh.write("x /= y; authorizeLaunch(z);\n")
    fh.write("// chain-note\u2028hostExecutor\n")
    fh.write("// assign-note\u2029authorizeLaunch\n")
    fh.write("const v = total / HostExecutor / 2;\n")
EOF
}

mut_interpolation_in_required() {
  local d="$1"
  local f="$d/app/packages/adapter-utils/src/server-utils.ts"
  sed -i -e 's/hostExecutor/XXXXXXXXXXXX/g' \
         -e 's/HostExecutor/XXXXXXXXXXXX/g' \
         -e 's/authorizeLaunch/XXXXXXXXXXXXXXX/g' "$f"
  python3 - "$f" <<'EOF'
import sys
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write("const s1 = `${hostExecutor}`;\n")
    fh.write("const s2 = `outer ${`inner ${hostExecutor}`}`;\n")
    fh.write("const s3 = `${hostExecutor + 1}`;\n")
    fh.write("const s4 = `${HostExecutor}`;\n")
    fh.write("const s5 = `${authorizeLaunch(x)}`;\n")
EOF
}

# want: <name> <mutator> <hostExecutor_n> <HostExecutor_n> <authorizeLaunch_n>
run_required_forms_case() {
  local name="$1" mut="$2" n_host="$3" n_type="$4" n_auth="$5"
  local dir="$FIXROOT/$name"
  build_fixture "$dir"
  "$mut" "$dir"

  local json rc
  set +e
  json="$(python3 "$GATE" --root "$dir" --json 2>&1)"
  rc=$?
  set -e

  local ok=1
  [ "$rc" -eq 0 ] || ok=0
  python3 - "$json" "$n_host" "$n_type" "$n_auth" <<'EOF' || ok=0
import json, sys
d = json.loads(sys.argv[1])
want_host, want_type, want_auth = int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
assert d["failures"] == 0, d
assert d["audit_errors"] == 0, d
ep = {e["symbol"]: e for e in d["expected_present"]}
assert ep["hostExecutor"]["occurrences"] == want_host, ep
assert ep["HostExecutor"]["occurrences"] == want_type, ep
assert ep["authorizeLaunch"]["occurrences"] == want_auth, ep
assert all(e["verdict"] == "present_as_expected" for e in ep.values()), ep
EOF

  if [ "$ok" -eq 1 ]; then
    echo "  [PASS] $name (exit=$rc, counts $n_host/$n_type/$n_auth)"
    pass=$((pass + 1))
  else
    echo "  [FAIL] $name: wanted exit=0 with counts $n_host/$n_type/$n_auth; got exit=$rc"
    printf '%s\n' "$json" | sed 's/^/         | /'
    fail=$((fail + 1))
  fi
}

run_required_forms_case "division_forms_count_in_required" mut_division_in_required 2 1 2
run_required_forms_case "interpolation_forms_count_in_required" mut_interpolation_in_required 3 1 1

mut_division_in_forbidden() {
  local d="$1"
  python3 - "$d/app/packages/adapter-utils/src/ssh.ts" <<'EOF'
import sys
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write("const r = total / hostExecutor / 2;\n")
    fh.write("x /= y; authorizeLaunch(z);\n")
    fh.write("// trailing-note\u2028HostExecutor\n")
EOF
}

mut_interpolation_in_forbidden() {
  local d="$1"
  python3 - "$d/app/packages/adapter-utils/src/ssh.ts" <<'EOF'
import sys
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write("const s1 = `${hostExecutor}`;\n")
    fh.write("const s4 = `${HostExecutor}`;\n")
    fh.write("const s5 = `${authorizeLaunch(x)}`;\n")
EOF
}

run_forbidden_forms_case() {
  local name="$1"; shift
  local dir="$FIXROOT/$name"
  build_fixture "$dir"
  "$@" "$dir"

  local json rc
  set +e
  json="$(python3 "$GATE" --root "$dir" --json 2>&1)"
  rc=$?
  set -e

  local ok=1
  [ "$rc" -eq 1 ] || ok=0
  python3 - "$json" <<'EOF' || ok=0
import json, sys
d = json.loads(sys.argv[1])
assert d["failures"] == 3, d
assert d["audit_errors"] == 0, d
ep = {e["symbol"]: e for e in d["expected_present"]}
for sym in ("hostExecutor", "HostExecutor", "authorizeLaunch"):
    assert ep[sym]["verdict"] == "unexpectedly_present_in_ssh", ep
    assert ep[sym]["ssh_occurrences"] == 1, ep
EOF

  if [ "$ok" -eq 1 ]; then
    echo "  [PASS] $name (exit=$rc, all 3 symbols caught in ssh.ts)"
    pass=$((pass + 1))
  else
    echo "  [FAIL] $name: wanted exit=1 with all 3 unexpectedly_present_in_ssh at ssh=1; got exit=$rc"
    printf '%s\n' "$json" | sed 's/^/         | /'
    fail=$((fail + 1))
  fi
}

run_forbidden_forms_case "division_forms_fail_in_forbidden" mut_division_in_forbidden
run_forbidden_forms_case "interpolation_forms_fail_in_forbidden" mut_interpolation_in_forbidden

# A regex is atomic in both modes: body punctuation (quote, //, backtick, })
# must not desync the scan and hide a seam after it in ssh.ts.
mut_regex_quote_in_forbidden() {
  local d="$1"
  python3 - "$d/app/packages/adapter-utils/src/ssh.ts" <<'EOF'
import sys
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write("const q = /\\'/; const leak = hostExecutor;\n")
    fh.write("const u = /https:\\/\\/\\//; authorizeLaunch(u);\n")
    fh.write("if (/['\"]/.test(s)) HostExecutor.boot();\n")
EOF
}

mut_regex_desync_in_forbidden() {
  local d="$1"
  python3 - "$d/app/packages/adapter-utils/src/ssh.ts" <<'EOF'
import sys
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write("const t = /a`b/;\n")
    fh.write("leak2(hostExecutor);\n")
    fh.write("const m = `${/}/.source, authorizeLaunch}`;\n")
    fh.write("const c = /\\/\\//; HostExecutor(c);\n")
EOF
}

# Fail-closed half of the atomic-regex contract: a seam inside a regex body
# in ssh.ts is still counted (the required file skips it; see the
# lexical_regex_only_absent negative case).
mut_regex_body_seam_in_forbidden() {
  local d="$1"
  python3 - "$d/app/packages/adapter-utils/src/ssh.ts" <<'EOF'
import sys
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write("const r1 = /hostExecutor/.test(s);\n")
    fh.write("const r2 = /see:HostExecutor/.test(s);\n")
    fh.write("const r3 = /authorizeLaunch/i.test(s);\n")
EOF
}

run_forbidden_forms_case "regex_quote_forms_fail_in_forbidden" mut_regex_quote_in_forbidden
run_forbidden_forms_case "regex_desync_forms_fail_in_forbidden" mut_regex_desync_in_forbidden
run_forbidden_forms_case "regex_body_seam_fails_closed_in_forbidden" mut_regex_body_seam_in_forbidden

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
