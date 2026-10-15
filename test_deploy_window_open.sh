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

printf 'PASS: deploy window wrapper contract\n'
