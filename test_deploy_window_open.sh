#!/usr/bin/env bash
# Offline contract test for deploy_window_open.sh. No host path or network is used.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"
export TMP
trap 'rm -rf "$TMP"' EXIT

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
pass() { printf 'ok: %s\n' "$*"; }

# The production entry point must stay bound to the literal operator path.
grep -Fq 'HOST_GATE="/paperclip/operator-handoff/deploy-window-manifest.py"' \
  "$ROOT/deploy_window_open.sh" || fail 'HOST_GATE literal path changed'

# Copy the real wrapper and replace only its constant in this disposable tree.
mkdir -p "$TMP/wrapper/verification" "$TMP/operator-handoff"
cp "$ROOT/deploy_window_open.sh" "$TMP/wrapper/deploy_window_open.sh"
python3 - "$TMP/wrapper/deploy_window_open.sh" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
s = p.read_text()
old = 'HOST_GATE="/paperclip/operator-handoff/deploy-window-manifest.py"'
new = 'HOST_GATE="' + str(Path(sys.argv[1]).parent.parent / 'operator-handoff/deploy-window-manifest.py') + '"'
if s.count(old) != 1:
    raise SystemExit('HOST_GATE replacement anchor is not unique')
p.write_text(s.replace(old, new))
PY
chmod +x "$TMP/wrapper/deploy_window_open.sh"
DRIFT="$TMP/wrapper/verification/case_5dfa9150-handoff-copy-drift.sh"
GATE="$TMP/operator-handoff/deploy-window-manifest.py"

write_drift() {
  local status="$1"
  printf '#!/usr/bin/env bash\nprintf "drift\\n" >> "$TMP/drift.calls"\nexit %s\n' "$status" > "$DRIFT"
  chmod +x "$DRIFT"
}
write_gate() {
  local status="$1"
  cat > "$GATE" <<EOF
#!/usr/bin/env python3
import json, os, sys
with open(os.environ['GATE_LOG'], 'w') as f:
    json.dump(sys.argv[1:], f)
sys.exit($status)
EOF
  chmod +x "$GATE"
}
run_wrapper() {
  local expected="$1" output rc
  shift
  set +e
  output="$(GATE_LOG="$TMP/gate.args" "$TMP/wrapper/deploy_window_open.sh" "$@" 2>&1)"
  rc=$?
  set -e
  [[ "$rc" -eq "$expected" ]] || fail "expected exit $expected, got $rc; output: $output"
  printf '%s' "$output"
}
assert_not_called() {
  [[ ! -e "$TMP/gate.args" ]] || fail 'host gate was executed unexpectedly'
}

# Drift failure must stop before the gate and must state why. The stub gate
# exists here on purpose: a wrapper that runs the gate before the drift check
# would execute it, so an absent argv log plus no step-2 output pins the order.
write_drift 1
write_gate 0
rm -f "$TMP/gate.args"
output="$(run_wrapper 1 --fixture drift)"
[[ "$output" == *'REFUSING to run the window gate'* ]] || fail 'drift refusal line missing'
[[ "$output" != *'step 2/2'* ]] || fail 'gate step ran despite drift failure'
assert_not_called
pass 'drift failure refuses and does not invoke gate'

# A clean drift check permits the gate; its status and message are preserved.
write_drift 0
for status in 0 1 2; do
  rm -f "$TMP/gate.args"
  write_gate "$status"
  output="$(run_wrapper "$status" 'arg with spaces' 'literal;$HOME' --flag=one)"
  [[ -e "$TMP/gate.args" ]] || fail "gate exit $status did not invoke gate"
  python3 - "$TMP/gate.args" <<'PY' || fail 'gate arguments changed'
import json, sys
with open(sys.argv[1]) as f:
    got = json.load(f)
want = ['arg with spaces', 'literal;$HOME', '--flag=one']
if got != want:
    raise SystemExit(f'expected {want!r}, got {got!r}')
PY
  if [[ "$status" -eq 0 ]]; then
    [[ "$output" == *'WINDOW OPEN:'* ]] || fail 'WINDOW OPEN line missing'
  else
    [[ "$output" == *"WINDOW CLOSED (gate exit $status):"* ]] || fail "WINDOW CLOSED exit $status line missing"
  fi
  pass "gate exit $status, status message, and arguments preserved"
done

# The wrapper must refuse to evaluate an absent or non-executable drift check.
rm -f "$DRIFT" "$TMP/gate.args"
output="$(run_wrapper 2)"
[[ "$output" == *'drift check missing or not executable'* ]] || fail 'missing drift refusal missing'
assert_not_called
write_drift 0
chmod 644 "$DRIFT"
output="$(run_wrapper 2)"
[[ "$output" == *'drift check missing or not executable'* ]] || fail 'non-executable drift refusal missing'
assert_not_called
pass 'missing and non-executable drift checks refuse with exit 2'

# --- end to end with the REAL drift check ---------------------------------------------
# The stubs above pin ordering and exit codes. These cases pin which BYTES run: the
# wrapper must check a private copy and run that same copy, isolated from the shared
# handoff directory. The real checker is re-pinned to a synthetic reviewed gate held in
# a throwaway object store, as verification/case_c89b5362-drift-pin-mutation-gate.sh does.
E2E="$TMP/e2e"
HANDOFF="$E2E/operator-handoff"
mkdir -p "$E2E/repo/verification" "$HANDOFF" "$E2E/shim"
git -C "$E2E/repo" init -q
sed "s|^HOST_GATE=.*|HOST_GATE=\"$HANDOFF/deploy-window-manifest.py\"|" \
  "$ROOT/deploy_window_open.sh" > "$E2E/repo/deploy_window_open.sh"
chmod +x "$E2E/repo/deploy_window_open.sh"

cat > "$E2E/reviewed.py" <<'EOF'
#!/usr/bin/env python3
# reviewed synthetic gate
import json, os, stat, sys
here = os.path.dirname(os.path.abspath(sys.argv[0]))
with open(os.environ['GATE_LOG'], 'w') as f:
    json.dump({'ran': 'reviewed', 'argv': sys.argv[1:], 'dir': here,
               'dir_mode': oct(stat.S_IMODE(os.stat(here).st_mode)),
               'isolated': sys.flags.isolated}, f)
EOF
cat > "$E2E/swapped.py" <<'EOF'
#!/usr/bin/env python3
import json, os
with open(os.environ['GATE_LOG'], 'w') as f:
    json.dump({'ran': 'swapped'}, f)
EOF
blob="$(git -C "$E2E/repo" hash-object -w "$E2E/reviewed.py")"
sha="$(sha256sum "$E2E/reviewed.py" | cut -d' ' -f1)"
sed -e "s/^PINNED_BLOB=\"[0-9a-f]\{40\}\"$/PINNED_BLOB=\"$blob\"/" \
    -e "s/^PINNED_SHA256=\"[0-9a-f]\{64\}\"$/PINNED_SHA256=\"$sha\"/" \
  "$ROOT/verification/case_5dfa9150-handoff-copy-drift.sh" \
  > "$E2E/repo/verification/case_5dfa9150-handoff-copy-drift.sh"
chmod +x "$E2E/repo/verification/case_5dfa9150-handoff-copy-drift.sh"
grep -Fq "PINNED_BLOB=\"$blob\"" "$E2E/repo/verification/case_5dfa9150-handoff-copy-drift.sh" ||
  fail 'could not re-pin the staged drift check'

# Anything the handoff directory could inject into the gate's imports leaves a marker.
for mod in json os stat; do
  printf 'open("%s", "w").write("imported")\n' "$E2E/planted.$mod" > "$HANDOFF/$mod.py"
done

# A python3 shim that, when the wrapper starts the gate, swaps the handoff file for
# other bytes first: the latest possible moment after the drift check passed.
REAL_PYTHON="$(command -v python3)"
cat > "$E2E/shim/python3" <<EOF
#!/usr/bin/env bash
if [ -n "\${SWAP_IN:-}" ]; then cp "\$SWAP_IN" "$HANDOFF/deploy-window-manifest.py"; fi
exec "$REAL_PYTHON" "\$@"
EOF
chmod +x "$E2E/shim/python3"

run_e2e() { # expected-exit [env assignments...] -- [args...]
  local expected="$1" output rc
  shift
  local -a envs=()
  while [[ "$#" -gt 0 && "$1" != '--' ]]; do envs+=("$1"); shift; done
  shift
  rm -f "$E2E/gate.log"
  set +e
  # -u BASH_ENV: a startup file that resets PATH would bypass the shim below.
  output="$(cd "$HANDOFF" && env -u BASH_ENV GATE_LOG="$E2E/gate.log" PATH="$E2E/shim:$PATH" \
    PYTHONPATH="$HANDOFF" "${envs[@]}" "$E2E/repo/deploy_window_open.sh" "$@" 2>&1)"
  rc=$?
  set -e
  [[ "$rc" -eq "$expected" ]] || fail "expected exit $expected, got $rc; output: $output"
  printf '%s' "$output"
}
gate_log() { python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$E2E/gate.log" "$1"; }
reset_handoff() { cp "$E2E/reviewed.py" "$HANDOFF/deploy-window-manifest.py"; chmod 755 "$HANDOFF/deploy-window-manifest.py"; }

# Planted modules beside the handoff file, in the working directory and on PYTHONPATH
# are never imported, and the private copy is what runs.
reset_handoff
output="$(run_e2e 0 -- 'arg one' --flag)"
[[ "$output" == *'PASS: handoff copy matches'* ]] || fail "real drift check did not pass: $output"
[[ "$output" == *'WINDOW OPEN:'* ]] || fail 'WINDOW OPEN line missing'
for mod in json os stat; do
  [[ ! -e "$E2E/planted.$mod" ]] || fail "planted $mod.py beside the handoff file was imported"
done
[[ "$(gate_log ran)" == reviewed ]] || fail 'reviewed gate did not run'
[[ "$(gate_log argv)" == "['arg one', '--flag']" ]] || fail "gate arguments changed: $(gate_log argv)"
[[ "$(gate_log isolated)" == 1 ]] || fail 'gate did not run in isolated mode'
[[ "$(gate_log dir_mode)" == 0o700 ]] || fail "private copy dir mode is $(gate_log dir_mode), not 0700"
private_dir="$(gate_log dir)"
[[ "$private_dir" != "$HANDOFF" ]] || fail 'gate ran from the handoff directory'
[[ ! -e "$private_dir" ]] || fail "private copy was not removed: $private_dir"
pass 'planted modules beside the handoff file are not imported; private copy removed'

# Swapping the handoff file after the check does not change what runs.
reset_handoff
output="$(run_e2e 0 SWAP_IN="$E2E/swapped.py" --)"
cmp -s "$E2E/swapped.py" "$HANDOFF/deploy-window-manifest.py" || fail 'swap fixture did not swap'
[[ "$(gate_log ran)" == reviewed ]] || fail 'bytes swapped in after the check were run'
pass 'handoff file swapped after the check does not change what runs'

# Real drift still refuses, and names the handoff path the operator must sync.
reset_handoff
printf '# drift\n' >> "$HANDOFF/deploy-window-manifest.py"
output="$(run_e2e 1 --)"
[[ "$output" == *'REFUSING to run the window gate'* ]] || fail 'drift refusal line missing'
[[ ! -e "$E2E/gate.log" ]] || fail 'gate ran despite drift'
[[ "$output" == *"> \"$HANDOFF/deploy-window-manifest.py\""* ]] ||
  fail "sync instruction does not name the handoff path: $output"
rm -f "$HANDOFF/deploy-window-manifest.py"
output="$(run_e2e 1 --)"
[[ "$output" == *"handoff copy missing: $HANDOFF/deploy-window-manifest.py"* ]] ||
  fail "missing handoff copy not reported by its path: $output"
[[ ! -e "$E2E/gate.log" ]] || fail 'gate ran with no handoff copy'
pass 'drift and a missing handoff copy refuse and name the handoff path'

printf 'PASS: deploy window wrapper contract\n'
