#!/usr/bin/env bash
# ===========================================================================
# Offline suite for migration_rehearsal.sh (shared migration gates).
#
# Fully offline: fixture migrations plus fake provider/apply CLIs invoked by
# absolute path (no PATH lookup, so the suite is hermetic on locked-down
# runners too), no cloud, no database, no network. Proves plan digests, run
# applies through the preamble injector in order, evidence freshness/expiry,
# and the refusal paths (unknown provider, missing credentials, redaction).
# ===========================================================================
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="$HERE/migration_rehearsal.sh"
PASS=0; FAIL=0
ok() { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1"; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/migs" "$WORK/bin"

printf -- '-- phase: expand\nCREATE INDEX CONCURRENTLY a_idx ON t (a);\n' > "$WORK/migs/0042_a.sql"
printf -- '-- phase: expand\nCREATE INDEX CONCURRENTLY b_idx ON t (b);\n' > "$WORK/migs/0043_b.sql"

# Fake provider CLIs, invoked by ABSOLUTE path (never PATH lookup): they
# record argv, always succeed, and echo a token= line the way a chatty
# provider CLI might — the gate must redact it before it reaches output.
cat > "$WORK/bin/fake-pscale" <<'FAKE'
#!/usr/bin/env bash
printf '%s\n' "$@" >> "$REC/pscale.argv"
echo "fake-pscale: $* (token=SENTINELTOKEN-must-never-reach-output)"
FAKE
cat > "$WORK/bin/fake-restore" <<'FAKE'
#!/usr/bin/env bash
printf '%s\n' "$@" >> "$REC/restore.argv"
echo "fake-restore: $* (token=SENTINELTOKEN-must-never-reach-output)"
FAKE
# Fake apply: records the wrapped file it received and its first line.
cat > "$WORK/bin/fake-apply" <<'FAKE'
#!/usr/bin/env bash
printf '%s\n' "$1" >> "$REC/apply.argv"
head -n 1 "$1" >> "$REC/apply.firstlines"
FAKE
chmod +x "$WORK/bin/fake-pscale" "$WORK/bin/fake-restore" "$WORK/bin/fake-apply"
export REC="$WORK/rec"; mkdir -p "$REC"

# 1. plan enumerates the chain with digests, touching no cloud.
"$GATE" plan --migrations-dir "$WORK/migs" --provider planetscale --out "$WORK/plan.json" >/dev/null 2>&1 \
  && ok "plan enumerates the chain offline" \
  || bad "plan enumerates the chain offline"
python3 - "$WORK/plan.json" "$WORK/migs" <<'EOF'
import hashlib, json, sys
from pathlib import Path
doc = json.load(open(sys.argv[1]))
assert doc["status"] == "planned", doc
assert doc["provider"] == "planetscale", doc
want = {p.stem: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in Path(sys.argv[2]).glob("*.sql")}
assert {m["tag"]: m["sha256"] for m in doc["migrations"]} == want
assert [m["tag"] for m in doc["migrations"]] == sorted(want), "chain order"
EOF
[[ $? -eq 0 ]] \
  && ok "plan digests match file content in sorted order" \
  || bad "plan digests match file content in sorted order"
"$GATE" plan --migrations-dir "$WORK/migs" --provider oracle --out "$WORK/x.json" >/dev/null 2>&1 \
  && bad "plan refuses an unknown provider" || ok "plan refuses an unknown provider"

# 2. run refuses without credentials; the full rehearsal runs through the
# pitr provider with absolute-path fake CLIs (no PATH lookup, so the suite
# is hermetic on locked-down runners too).
if "$GATE" run --migrations-dir "$WORK/migs" --provider planetscale \
    --out "$WORK/e.json" >/dev/null 2>&1; then
  bad "run refuses without provider credentials"
else
  ok "run refuses without provider credentials"
fi
export REHEARSAL_PITR_ENDPOINT="https://example.invalid"
export REHEARSAL_RESTORE_CMD="$WORK/bin/fake-restore"
export REHEARSAL_APPLY_CMD="$WORK/bin/fake-apply"
run_out="$("$GATE" run --migrations-dir "$WORK/migs" --provider pitr \
  --branch "rehearsal/test-001" --out "$WORK/evidence.json" 2>&1)" \
  && ok "run applies the chain on the fake provider" \
  || bad "run applies the chain on the fake provider"
grep -q "SENTINELTOKEN-must-never-reach-output" <<< "$run_out" \
  && bad "run redacts credential values from output" \
  || ok "run redacts credential values from output"
grep -q "rehearsal/test-001" "$REC/restore.argv" \
  && ok "run restores the named rehearsal branch" \
  || bad "run restores the named rehearsal branch"
[[ "$(wc -l < "$REC/apply.argv")" -eq 2 ]] \
  && ok "run applies every migration in the chain" \
  || bad "run applies every migration in the chain"
[[ "$(sort -u "$REC/apply.firstlines" | wc -l)" -eq 1 ]] \
  && grep -q "SET lock_timeout" "$REC/apply.firstlines" \
  && ok "run applies files wrapped with the timeout preamble" \
  || bad "run applies files wrapped with the timeout preamble"
python3 - "$WORK/evidence.json" <<'EOF'
import json, sys
doc = json.load(open(sys.argv[1]))
assert doc["status"] == "passed", doc
assert doc["cleanup"] == "deleted", doc
assert doc["branch"] == "rehearsal/test-001", doc
assert [m["result"] for m in doc["migrations"]] == ["applied", "applied"], doc
EOF
[[ $? -eq 0 ]] \
  && ok "run writes passed evidence with per-migration results" \
  || bad "run writes passed evidence with per-migration results"

# 3. verify: fresh passes, stale/failed/unreadable do not.
"$GATE" verify --evidence "$WORK/evidence.json" --max-age-hours 24 >/dev/null 2>&1 \
  && ok "verify passes fresh passed evidence" \
  || bad "verify passes fresh passed evidence"
python3 - "$WORK/evidence.json" <<'EOF'
import json, sys
p = sys.argv[1]
doc = json.load(open(p))
doc["finished_at"] = "2020-01-01T00:00:00Z"
json.dump(doc, open(p, "w"))
EOF
"$GATE" verify --evidence "$WORK/evidence.json" --max-age-hours 24 >/dev/null 2>&1 \
  && bad "verify fails stale evidence" || ok "verify fails stale evidence"
"$GATE" verify --evidence "$WORK/missing.json" >/dev/null 2>&1 \
  && bad "verify refuses unreadable evidence" || ok "verify refuses unreadable evidence"

# 4. pitr refuses without its restore command.
(unset REHEARSAL_RESTORE_CMD; REHEARSAL_PITR_ENDPOINT="https://example.invalid" \
 "$GATE" run --migrations-dir "$WORK/migs" --provider pitr --out "$WORK/p.json" >/dev/null 2>&1) \
  && bad "pitr refuses without a restore command" || ok "pitr refuses without a restore command"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
