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
set -euo pipefail
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
if [[ "$1 $2" == "branch create" ]]; then
  [[ "${FAKE_PSCALE_FAIL_CREATE:-}" != 1 ]] || exit 1
  if [[ "${FAKE_PSCALE_BLOCK_CREATE:-}" == 1 ]]; then
    : > "$REC/create-started"
    while :; do sleep 1; done
  fi
fi
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
printf '%s\n' "$1" >> "$REC/apply.branch"
printf '%s\n' "$2" >> "$REC/apply.argv"
head -n 1 "$2" >> "$REC/apply.firstlines"
FAKE
chmod +x "$WORK/bin/fake-pscale" "$WORK/bin/fake-restore" "$WORK/bin/fake-apply"
export REC="$WORK/rec"; mkdir -p "$REC"

# 1. plan enumerates the chain with digests, touching no cloud.
"$GATE" plan --migrations-dir "$WORK/migs" --provider planetscale --out "$WORK/plan.json" >/dev/null 2>&1 \
  && ok "plan enumerates the chain offline" \
  || bad "plan enumerates the chain offline"
if python3 - "$WORK/plan.json" "$WORK/migs" <<'EOF'
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
then
  ok "plan digests match file content in sorted order"
else
  bad "plan digests match file content in sorted order"
fi
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
export REHEARSAL_RESTORE_CMD="$WORK/bin/fake-restore --at '2026-10-16 01:00:00'"
export REHEARSAL_APPLY_CMD="$WORK/bin/fake-apply {branch}"
run_out="$("$GATE" run --migrations-dir "$WORK/migs" --provider pitr \
  --branch "rehearsal-test-001" --out "$WORK/evidence.json" 2>&1)" \
  && ok "run applies the chain on the fake provider" \
  || bad "run applies the chain on the fake provider"
grep -q "SENTINELTOKEN-must-never-reach-output" <<< "$run_out" \
  && bad "run redacts credential values from output" \
  || ok "run redacts credential values from output"
grep -q "rehearsal-test-001" "$REC/restore.argv" \
  && ok "run restores the named rehearsal branch" \
  || bad "run restores the named rehearsal branch"
grep -Fxq "2026-10-16 01:00:00" "$REC/restore.argv" \
  && ok "PITR restore preserves quoted arguments" \
  || bad "PITR restore preserves quoted arguments"
grep -q "^rehearsal-test-001$" "$REC/apply.branch" \
  && ok "apply command receives the created branch as its explicit target" \
  || bad "apply command receives the created branch as its explicit target"
[[ "$(wc -l < "$REC/apply.argv")" -eq 2 ]] \
  && ok "run applies every migration in the chain" \
  || bad "run applies every migration in the chain"
[[ "$(sort -u "$REC/apply.firstlines" | wc -l)" -eq 1 ]] \
  && grep -q "SET lock_timeout" "$REC/apply.firstlines" \
  && ok "run applies files wrapped with the timeout preamble" \
  || bad "run applies files wrapped with the timeout preamble"
if python3 - "$WORK/evidence.json" <<'EOF'
import json, sys
doc = json.load(open(sys.argv[1]))
assert doc["status"] == "passed", doc
assert doc["cleanup"] == "delegated", doc
assert doc["branch"] == "rehearsal-test-001", doc
assert [m["result"] for m in doc["migrations"]] == ["applied", "applied"], doc
EOF
then
  ok "run writes passed evidence with per-migration results"
else
  bad "run writes passed evidence with per-migration results"
fi

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

# 5. Fail-fast: the first failed apply stops the chain. 0052 must never be
# applied after 0051 fails; it records not_attempted and the run fails.
mkdir -p "$WORK/migs_fail" "$WORK/rec2"
printf -- '-- phase: expand\nSELECT 1;\n' > "$WORK/migs_fail/0050_ok.sql"
printf -- '-- phase: expand\nSELECT 2;\n' > "$WORK/migs_fail/0051_boom.sql"
printf -- '-- phase: expand\nSELECT 3;\n' > "$WORK/migs_fail/0052_never.sql"
cat > "$WORK/bin/fake-apply-fail" <<'FAKE'
#!/usr/bin/env bash
printf '%s\n' "$1" >> "$REC/apply.branch"
printf '%s\n' "$2" >> "$REC/apply.argv"
case "$2" in *0051_boom*) exit 1 ;; *) exit 0 ;; esac
FAKE
chmod +x "$WORK/bin/fake-apply-fail"
export REC="$WORK/rec2"
export REHEARSAL_RESTORE_CMD="$WORK/bin/fake-restore"
export REHEARSAL_APPLY_CMD="$WORK/bin/fake-apply-fail {branch}"
if "$GATE" run --migrations-dir "$WORK/migs_fail" --provider pitr \
    --branch "rehearsal-fail-001" --out "$WORK/fail-evidence.json" >/dev/null 2>&1; then
  bad "run fails when an apply fails"
else
  ok "run fails when an apply fails"
fi
if grep -q "0052_never" "$REC/apply.argv"; then
  bad "no migration is applied after a failure"
else
  [[ "$(wc -l < "$REC/apply.argv")" -eq 2 ]] \
    && ok "no migration is applied after a failure" \
    || bad "no migration is applied after a failure"
fi
if python3 - "$WORK/fail-evidence.json" <<'EOF'
import json, sys
doc = json.load(open(sys.argv[1]))
assert doc["status"] == "failed", doc
assert [(m["tag"], m["result"]) for m in doc["migrations"]] == [
    ("0050_ok", "applied"), ("0051_boom", "failed"), ("0052_never", "not_attempted")], doc
EOF
then
  ok "failed run records failed/not_attempted per migration"
else
  bad "failed run records failed/not_attempted per migration"
fi

# 6. PlanetScale clone cleanup runs on an unexpected post-create exit.
mkdir -p "$WORK/cleanup"
cp "$GATE" "$WORK/cleanup/migration_rehearsal.sh"
chmod +x "$WORK/cleanup/migration_rehearsal.sh"
export REC="$WORK/rec-cleanup"; mkdir -p "$REC"
set +e
REHEARSAL_PSCALE_ORG=fake REHEARSAL_PSCALE_DB=fake \
  PLANETSCALE_SERVICE_TOKEN_ID= PLANETSCALE_SERVICE_TOKEN= \
  PSCALE_BIN="$WORK/bin/fake-pscale" \
  REHEARSAL_APPLY_CMD="$WORK/bin/fake-apply {branch}" \
  "$WORK/cleanup/migration_rehearsal.sh" run --migrations-dir "$WORK/migs" \
    --provider planetscale --branch "rehearsal-cleanup-001" --out "$WORK/cleanup.json" \
    >"$WORK/cleanup-output" 2>&1
cleanup_rc=$?
set -e
cleanup_out="$(cat "$WORK/cleanup-output")"
# The script uses the explicit fake PSCALE_BIN above; no PATH mutation or
# exported shell function is needed.
if [[ "$cleanup_rc" -eq 0 ]]; then
  bad "unexpected exit after branch creation fails"
else
  if [[ ! -e "$REC/pscale.argv" ]]; then
    printf '%s\n' "$cleanup_out"
  elif ! grep -q '^create$' "$REC/pscale.argv" || ! grep -q '^delete$' "$REC/pscale.argv"; then
    printf 'pscale argv log:\n'; cat "$REC/pscale.argv"
    printf 'rehearsal output:\n%s\n' "$cleanup_out"
  fi
  grep -q '^auth$' "$REC/pscale.argv" \
    && grep -q '^create$' "$REC/pscale.argv" \
    && grep -q '^delete$' "$REC/pscale.argv" \
    && ok "PSCALE_BIN authenticates and deletes the branch on unexpected exit" \
    || bad "PSCALE_BIN authenticates and deletes the branch on unexpected exit"
fi

# A normal branch-create failure must not delete a branch the run did not create.
export REC="$WORK/rec-create-fail"; mkdir -p "$REC"
set +e
FAKE_PSCALE_FAIL_CREATE=1 env \
  REHEARSAL_PSCALE_ORG=fake REHEARSAL_PSCALE_DB=fake \
  PLANETSCALE_SERVICE_TOKEN_ID= PLANETSCALE_SERVICE_TOKEN= \
  PSCALE_BIN="$WORK/bin/fake-pscale" \
  "$GATE" run --migrations-dir "$WORK/migs" --provider planetscale \
    --branch "rehearsal-existing-001" --out "$WORK/create-fail.json" \
    >"$WORK/create-fail-output" 2>&1
create_fail_rc=$?
set -e
if [[ "$create_fail_rc" -ne 0 ]] && ! grep -q '^delete$' "$REC/pscale.argv"; then
  ok "failed branch creation does not delete an unowned branch"
else
  bad "failed branch creation does not delete an unowned branch"
fi

# SIGTERM during PlanetScale branch creation must still attempt cleanup.
if command -v setsid >/dev/null 2>&1; then
  export REC="$WORK/rec-signal"; mkdir -p "$REC"
  FAKE_PSCALE_BLOCK_CREATE=1 setsid env \
    REHEARSAL_PSCALE_ORG=fake REHEARSAL_PSCALE_DB=fake \
    PLANETSCALE_SERVICE_TOKEN_ID= PLANETSCALE_SERVICE_TOKEN= \
    PSCALE_BIN="$WORK/bin/fake-pscale" \
    REHEARSAL_APPLY_CMD="$WORK/bin/fake-apply {branch}" \
    "$GATE" run --migrations-dir "$WORK/migs" --provider planetscale \
      --branch "rehearsal-signal-001" --out "$WORK/signal.json" \
      >"$WORK/signal-output" 2>&1 &
  signal_pid=$!
  for _ in $(seq 1 100); do
    [[ -e "$REC/create-started" ]] && break
    sleep 0.05
  done
  if [[ ! -e "$REC/create-started" ]]; then
    kill -TERM -- "-$signal_pid" 2>/dev/null || true
    wait "$signal_pid" 2>/dev/null || true
    bad "test setup reaches branch creation before signal"
  else
    kill -TERM -- "-$signal_pid" 2>/dev/null || true
    set +e
    wait "$signal_pid" 2>/dev/null
    set -e
    grep -q '^delete$' "$REC/pscale.argv" \
      && ok "SIGTERM during branch creation attempts clone cleanup" \
      || bad "SIGTERM during branch creation attempts clone cleanup"
  fi
else
  bad "setsid is required to test SIGTERM during branch creation"
fi

# 7. --only filters the pending set but applies it in migration filename order;
# unknown tags are refused.
"$GATE" plan --migrations-dir "$WORK/migs" --provider pitr --only "0043_b" \
  --out "$WORK/plan-only.json" >/dev/null 2>&1 \
  && ok "plan accepts an --only subset" \
  || bad "plan accepts an --only subset"
if python3 - "$WORK/plan-only.json" <<'EOF'
import json, sys
doc = json.load(open(sys.argv[1]))
assert [m["tag"] for m in doc["migrations"]] == ["0043_b"], doc
EOF
then
  ok "plan --only keeps exactly the named migration"
else
  bad "plan --only keeps exactly the named migration"
fi
"$GATE" plan --migrations-dir "$WORK/migs" --provider pitr --only "0099_nope" \
  --out "$WORK/x.json" >/dev/null 2>&1 \
  && bad "plan refuses an --only tag outside the dir" \
  || ok "plan refuses an --only tag outside the dir"
if "$GATE" plan --migrations-dir "$WORK/migs" --provider pitr --only "0043_b,0042_a" \
    --out "$WORK/reversed-plan.json" >/dev/null 2>&1 \
    && python3 - "$WORK/reversed-plan.json" <<'EOF'
import json, sys
assert [m["tag"] for m in json.load(open(sys.argv[1]))["migrations"]] == ["0042_a", "0043_b"]
EOF
then
  ok "--only input order cannot change production migration order"
else
  bad "--only input order cannot change production migration order"
fi
export REC="$WORK/rec-order"; mkdir -p "$REC"
if "$GATE" run --migrations-dir "$WORK/migs" --provider pitr \
    --only "0043_b,0042_a" --branch "rehearsal-order-001" \
    --out "$WORK/reversed-evidence.json" >/dev/null 2>&1 \
    && python3 - "$WORK/reversed-evidence.json" "$REC/apply.argv" <<'EOF'
import json, sys
assert [m["tag"] for m in json.load(open(sys.argv[1]))["migrations"]] == ["0042_a", "0043_b"]
applied = open(sys.argv[2]).read().splitlines()
assert len(applied) == 2 and "0042_a" in applied[0] and "0043_b" in applied[1], applied
EOF
then
  ok "rehearsal applies and records --only tags in production order"
else
  bad "rehearsal applies and records --only tags in production order"
fi

# 7. verify binds evidence to the candidate chain and rejects a future
# finished_at. Fresh passing evidence for the two-migration dir first.
export REC="$WORK/rec"; mkdir -p "$REC"
export REHEARSAL_APPLY_CMD="$WORK/bin/fake-apply {branch}"
"$GATE" run --migrations-dir "$WORK/migs" --provider pitr \
  --branch "rehearsal-verify-001" --out "$WORK/verify-evidence.json" >/dev/null 2>&1 \
  || bad "setup: fresh passing run for verify binding"
(cd "$WORK/migs" && sha256sum ./*.sql) > "$WORK/candidate-hashes.txt"
"$GATE" verify --evidence "$WORK/verify-evidence.json" \
  --sql-hashes "$WORK/candidate-hashes.txt" --max-age-hours 24 >/dev/null 2>&1 \
  && ok "verify passes evidence bound to the candidate chain" \
  || bad "verify passes evidence bound to the candidate chain"
python3 - "$WORK/reversed-evidence.json" <<'EOF'
import json, sys
p = sys.argv[1]
doc = json.load(open(p))
doc["migrations"].reverse()
json.dump(doc, open(p, "w"))
EOF
"$GATE" verify --evidence "$WORK/reversed-evidence.json" \
  --sql-hashes "$WORK/candidate-hashes.txt" --max-age-hours 24 >/dev/null 2>&1 \
  && bad "verify fails evidence with the wrong migration order" \
  || ok "verify fails evidence with the wrong migration order"
# Same evidence against an unrelated chain must fail.
(cd "$WORK/migs_fail" && sha256sum ./*.sql) > "$WORK/other-hashes.txt"
"$GATE" verify --evidence "$WORK/verify-evidence.json" \
  --sql-hashes "$WORK/other-hashes.txt" --max-age-hours 24 >/dev/null 2>&1 \
  && bad "verify fails evidence for an unrelated chain" \
  || ok "verify fails evidence for an unrelated chain"
# A finished_at in the future (forged freshness) must fail, not print a
# negative age as ok.
python3 - "$WORK/verify-evidence.json" "$WORK/future-evidence.json" <<'EOF'
import json, sys
doc = json.load(open(sys.argv[1]))
doc["finished_at"] = "2099-01-01T00:00:00Z"
json.dump(doc, open(sys.argv[2], "w"))
EOF
"$GATE" verify --evidence "$WORK/future-evidence.json" --max-age-hours 24 >/dev/null 2>&1 \
  && bad "verify fails a future finished_at" \
  || ok "verify fails a future finished_at"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
