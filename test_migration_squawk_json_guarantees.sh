#!/usr/bin/env bash
# ===========================================================================
# Companion suite: pins the JSON gate's post-acceptance hardening guarantees.
#
# The immutable acceptance module (test_migration_squawk_json.sh) pins the
# envelope shape; this suite pins the behaviours the envelope relies on:
#
#   1. The JSON-mode Squawk deadline: a hung linter is killed after
#      SQUAWK_TIMEOUT_SECONDS (default 60) and the step fails with the fixed
#      "Squawk timed out." diagnostic -- even when the direct child already
#      exited 0 and only a descendant holds the output pipe.
#   2. The tag-only diagnostics projection: SQL excerpts and credential-like
#      prose from linter output never reach the JSON; tagless output becomes
#      the fixed "[unstructured diagnostics omitted]" marker.
#   3. The directory-discovery refusal envelopes: a missing, non-directory
#      or unreadable migrations dir refuses with a stable code and exit 2.
#   4. The JSON missing-option-value guard: --migrations-dir/--squawk-bin/
#      --squawk-config without a value refuse as invalid-input, exit 2.
#   5. JSON-mode --config forwarding: the configured squawk config reaches
#      the linter argv; --squawk-config "" disables it.
#
# HERMETIC BY CONSTRUCTION. Fixture migrations in a throwaway temp dir, fake
# squawk binaries, short SQUAWK_TIMEOUT_SECONDS deadlines with an outer
# `timeout` bound on every hang probe, no database, no network. Requires
# bash, jq, python3 and timeout(1). The unreadable-path probes assert real
# permission bits, so they skip loudly when run as root (root is not subject
# to the bits they measure); CI runners are unprivileged.
# ===========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="$HERE/migration_squawk.sh"
PASS=0; FAIL=0
ok() { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1"; }

command -v jq >/dev/null 2>&1 || { echo "test_migration_squawk_json_guarantees: jq is required" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "test_migration_squawk_json_guarantees: python3 is required" >&2; exit 2; }
command -v timeout >/dev/null 2>&1 || { echo "test_migration_squawk_json_guarantees: timeout(1) is required" >&2; exit 2; }
[[ -x "$GATE" ]] || { echo "test_migration_squawk_json_guarantees: $GATE is not executable" >&2; exit 2; }

# jq predicate over a captured stdout file; prints $3 on failure only.
jassert() {
  local filter="$1" file="$2" label="$3" diag="${4:-}"
  if jq -e "$filter" "$file" >/dev/null 2>&1; then
    ok "$label"
  else
    bad "$label"
    [[ -n "$diag" ]] && printf '        %s\n' "$diag"
  fi
  return 0
}

# Exactly one JSON document on stdout: python's json.loads rejects trailing
# bytes that a streaming jq read would silently accept as a second document.
single_doc() {
  python3 -c 'import sys,json; json.loads(open(sys.argv[1]).read())' "$1" >/dev/null 2>&1
  return $?
}

WORK="$(mktemp -d)"
cleanup() { chmod -R u+rwX "$WORK" 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT
mkdir -p "$WORK/migs" "$WORK/bin"
OUT="$WORK/out"; ERR="$WORK/err"; mkdir -p "$OUT" "$ERR"

# Distinctive tokens: PROBE lives in fixture DDL and shim output, CRED in
# shim output only; any occurrence in JSON output proves a leak.
PROBE="zz_guarantee_body_probe"
CRED="s3cret-guarantee-probe"

cat > "$WORK/migs/0100_clean.sql" <<EOF
-- phase: expand
CREATE INDEX CONCURRENTLY IF NOT EXISTS member_search_idx ON $PROBE (email);
ALTER TABLE $PROBE ADD CONSTRAINT members_email_format CHECK (email LIKE '%@%') NOT VALID;
EOF

# Deterministic squawk shims: hangs, leaks, and an argv recorder.
cat > "$WORK/bin/squawk-hang" <<FAKE
#!/usr/bin/env bash
echo \$\$ > "$WORK/hang.pid"
exec sleep 120
FAKE
cat > "$WORK/bin/squawk-partial-hang" <<'FAKE'
#!/usr/bin/env bash
echo "error: prefer-robust-stmts (0042_add_search.sql)"
exec sleep 120
FAKE
cat > "$WORK/bin/squawk-exit-holder" <<FAKE
#!/usr/bin/env bash
echo "warning: ban-drop-column (0042_add_search.sql)"
sleep 120 &
echo \$! > "$WORK/holder.pid"
exit 0
FAKE
cat > "$WORK/bin/squawk-leaky" <<FAKE
#!/usr/bin/env bash
echo "error: prefer-robust-stmts (0042_add_search.sql)"
echo "CREATE INDEX bad_probe_idx ON $PROBE (email);"
echo "connection failed: password=$CRED host=db.internal"
exit 1
FAKE
cat > "$WORK/bin/squawk-opaque" <<'FAKE'
#!/usr/bin/env bash
echo "squawk blew up in a way nobody predicted"
exit 1
FAKE
cat > "$WORK/bin/squawk-argv" <<FAKE
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$WORK/argv.txt"
exit 0
FAKE
chmod +x "$WORK"/bin/squawk-*
echo "# probe config" > "$WORK/custom.toml"

# Guard against vacuous no-leak assertions: the probes must really be there.
if grep -q "$PROBE" "$WORK/migs/0100_clean.sql" \
    && grep -q "$PROBE" "$WORK/bin/squawk-leaky" \
    && grep -q "$CRED" "$WORK/bin/squawk-leaky"; then
  ok "leak probes live in fixtures and shim output (no-leak checks are load-bearing)"
else
  bad "leak probes live in fixtures and shim output (no-leak checks are load-bearing)"
fi

# 1. Silent hang: the step fails at the deadline with the fixed diagnostic.
rm -f "$WORK/hang.pid"
rc=0
START=$SECONDS
SQUAWK_TIMEOUT_SECONDS=2 timeout 25s "$GATE" --format json --squawk-bin "$WORK/bin/squawk-hang" \
  "$WORK/migs/0100_clean.sql" >"$OUT/hang.json" 2>"$ERR/hang.txt" || rc=$?
ELAPSED=$((SECONDS - START))
[[ "$rc" -eq 1 ]] && ok "silent hang fails with exit 1 (deadline, not the 120s shim)" || bad "silent hang fails with exit 1 (got rc=$rc, ${ELAPSED}s)"
[[ "$ELAPSED" -lt 20 ]] && ok "silent hang returns near the 2s deadline (took ${ELAPSED}s)" || bad "silent hang returns near the 2s deadline (took ${ELAPSED}s)"
single_doc "$OUT/hang.json" \
  && ok "hang run prints exactly one JSON document" \
  || bad "hang run prints exactly one JSON document"
jassert '.verdict == "fail" and .files[0].verdict == "fail"' "$OUT/hang.json" \
  "hang verdict fails the run and the file"
jassert '.files[0].squawk.status == "fail"' "$OUT/hang.json" \
  "timed-out squawk step reads fail"
jassert '.files[0].squawk.diagnostics | contains("Squawk timed out.")' "$OUT/hang.json" \
  "timed-out step carries the fixed diagnostic"

# The deadline must kill the linter, not just stop reading it.
wait_dead() {
  local pidfile="$1" label="$2" pid="" i=0
  [[ -f "$pidfile" ]] || { bad "$label (no pid recorded)"; return 0; }
  pid="$(cat "$pidfile")"
  while kill -0 "$pid" 2>/dev/null && [[ "$i" -lt 25 ]]; do sleep 0.2; i=$((i+1)); done
  if kill -0 "$pid" 2>/dev/null; then bad "$label (pid $pid survived)"; else ok "$label"; fi
  return 0
}
wait_dead "$WORK/hang.pid" "the hung linter is reaped with the gate"

# 2. Partial output then hang: tags collected before the deadline survive it.
rc=0
SQUAWK_TIMEOUT_SECONDS=2 timeout 25s "$GATE" --format json --squawk-bin "$WORK/bin/squawk-partial-hang" \
  "$WORK/migs/0100_clean.sql" >"$OUT/partial.json" 2>/dev/null || rc=$?
[[ "$rc" -eq 1 ]] && ok "partial-then-hang fails with exit 1" || bad "partial-then-hang fails with exit 1 (got $rc)"
jassert '.files[0].squawk.status == "fail"' "$OUT/partial.json" \
  "partial-then-hang step reads fail"
jassert '.files[0].squawk.diagnostics | contains("Squawk timed out.")' "$OUT/partial.json" \
  "partial-then-hang keeps the timeout marker"
jassert '.files[0].squawk.diagnostics | contains("prefer-robust-stmts")' "$OUT/partial.json" \
  "tags collected before the deadline survive it"

# 3. Direct child exits 0 while a descendant holds the pipe: still a timeout fail.
rm -f "$WORK/holder.pid"
rc=0
SQUAWK_TIMEOUT_SECONDS=2 timeout 25s "$GATE" --format json --squawk-bin "$WORK/bin/squawk-exit-holder" \
  "$WORK/migs/0100_clean.sql" >"$OUT/holder.json" 2>/dev/null || rc=$?
[[ "$rc" -eq 1 ]] && ok "exited child with a live pipe heir fails with exit 1" || bad "exited child with a live pipe heir fails with exit 1 (got $rc)"
jassert '.files[0].squawk.status == "fail"' "$OUT/holder.json" \
  "a timed-out rc==0 step still reads fail"
jassert '.files[0].squawk.diagnostics | contains("Squawk timed out.")' "$OUT/holder.json" \
  "pipe-heir timeout keeps the fixed diagnostic"
jassert '.files[0].squawk.diagnostics | contains("ban-drop-column")' "$OUT/holder.json" \
  "the heir-held run keeps the tag printed before the exit"
wait_dead "$WORK/holder.pid" "the pipe-holding descendant is reaped with the gate"

# 4. The deadline override parses strictly: unset/bogus/empty/non-positive keep 60.
timeout_probe() {
  if env SQUAWK_TIMEOUT_SECONDS="$1" python3 -c \
    'import sys; sys.path.insert(0, sys.argv[1]); import migration_sql; sys.exit(0 if migration_sql.squawk_timeout_seconds() == float(sys.argv[2]) else 1)' \
    "$HERE" "$2"; then
    ok "$3"
  else
    bad "$3"
  fi
  return 0
}
timeout_probe 2.5 2.5 "a numeric override sets the deadline"
timeout_probe bogus 60 "a non-numeric override keeps the 60s default"
timeout_probe '' 60 "an empty override keeps the 60s default"
timeout_probe 0 60 "a zero override keeps the 60s default"
timeout_probe -3 60 "a negative override keeps the 60s default"
if env -u SQUAWK_TIMEOUT_SECONDS python3 -c \
  'import sys; sys.path.insert(0, sys.argv[1]); import migration_sql; sys.exit(0 if migration_sql.squawk_timeout_seconds() == 60 else 1)' \
  "$HERE"; then
  ok "an unset override keeps the 60s default"
else
  bad "an unset override keeps the 60s default"
fi

# 5. Leaky linter output: tags pass, SQL and credential prose never do.
rc=0
"$GATE" --format json --squawk-bin "$WORK/bin/squawk-leaky" \
  "$WORK/migs/0100_clean.sql" >"$OUT/leaky.json" 2>/dev/null || rc=$?
[[ "$rc" -eq 1 ]] && ok "leaky-squawk json run exits 1" || bad "leaky-squawk json run exits 1 (got $rc)"
single_doc "$OUT/leaky.json" \
  && ok "leaky run prints exactly one JSON document" \
  || bad "leaky run prints exactly one JSON document"
jassert '.files[0].squawk.status == "fail"' "$OUT/leaky.json" \
  "leaky squawk step reads fail"
jassert '.files[0].squawk.diagnostics | contains("prefer-robust-stmts")' "$OUT/leaky.json" \
  "the valid tag still passes through"
if grep -q "$PROBE" "$OUT/leaky.json"; then
  bad "SQL excerpts in linter output never reach the JSON"
else
  ok "SQL excerpts in linter output never reach the JSON"
fi
if grep -q "$CRED" "$OUT/leaky.json"; then
  bad "credential prose in linter output never reaches the JSON"
else
  ok "credential prose in linter output never reaches the JSON"
fi

# 6. Tagless linter output becomes the fixed marker, not echoed prose.
rc=0
"$GATE" --format json --squawk-bin "$WORK/bin/squawk-opaque" \
  "$WORK/migs/0100_clean.sql" >"$OUT/opaque.json" 2>/dev/null || rc=$?
[[ "$rc" -eq 1 ]] && ok "opaque-squawk json run exits 1" || bad "opaque-squawk json run exits 1 (got $rc)"
jassert '.files[0].squawk.diagnostics == "[unstructured diagnostics omitted]"' "$OUT/opaque.json" \
  "tagless output becomes the fixed marker"

# 7. Directory discovery refusals carry stable codes and exit 2.
rc=0
"$GATE" --format json --migrations-dir "$WORK/no-such-dir" >"$OUT/nodir.json" 2>/dev/null || rc=$?
[[ "$rc" -eq 2 ]] && ok "missing dir refuses with exit 2" || bad "missing dir refuses with exit 2 (got $rc)"
single_doc "$OUT/nodir.json" \
  && ok "missing-dir refusal is one JSON document" \
  || bad "missing-dir refusal is one JSON document"
jassert '.verdict == "refused" and .files == []' "$OUT/nodir.json" \
  "missing-dir refusal verdict is refused with no files"
jassert '.error.code == "migrations-dir-missing"' "$OUT/nodir.json" \
  "missing dir pins code migrations-dir-missing"

rc=0
"$GATE" --format json --migrations-dir "$WORK/migs/0100_clean.sql" >"$OUT/filedir.json" 2>/dev/null || rc=$?
[[ "$rc" -eq 2 ]] && ok "file-as-dir refuses with exit 2" || bad "file-as-dir refuses with exit 2 (got $rc)"
jassert '.verdict == "refused" and .error.code == "migrations-dir-missing"' "$OUT/filedir.json" \
  "a non-directory pins code migrations-dir-missing"

mkdir -p "$WORK/locked"
chmod 000 "$WORK/locked"
if [[ "$(id -u)" -eq 0 ]]; then
  ok "unreadable dir pins code migrations-dir-unreadable (skipped: root ignores permission bits)"
else
  rc=0
  "$GATE" --format json --migrations-dir "$WORK/locked" >"$OUT/locked.json" 2>/dev/null || rc=$?
  [[ "$rc" -eq 2 ]] && ok "unreadable dir refuses with exit 2" || bad "unreadable dir refuses with exit 2 (got $rc)"
  single_doc "$OUT/locked.json" \
    && ok "unreadable-dir refusal is one JSON document" \
    || bad "unreadable-dir refusal is one JSON document"
  jassert '.error.code == "migrations-dir-unreadable"' "$OUT/locked.json" \
    "unreadable dir pins code migrations-dir-unreadable"
fi
chmod 755 "$WORK/locked"

cp "$WORK/migs/0100_clean.sql" "$WORK/noread.sql"
chmod 000 "$WORK/noread.sql"
if [[ "$(id -u)" -eq 0 ]]; then
  ok "unreadable file pins code file-unreadable (skipped: root ignores permission bits)"
else
  rc=0
  "$GATE" --format json "$WORK/noread.sql" >"$OUT/noread.json" 2>/dev/null || rc=$?
  [[ "$rc" -eq 2 ]] && ok "unreadable file refuses with exit 2" || bad "unreadable file refuses with exit 2 (got $rc)"
  jassert '.error.code == "file-unreadable"' "$OUT/noread.json" \
    "unreadable file pins code file-unreadable"
fi
chmod 644 "$WORK/noread.sql"

# 8. A missing option value in JSON mode refuses as invalid-input, never prose.
rc=0
PATH="$WORK/bin:/usr/bin:/bin" SQUAWK_BIN="" "$GATE" --format json --migrations-dir \
  >"$OUT/guard-dir.json" 2>/dev/null || rc=$?
[[ "$rc" -eq 2 ]] && ok "missing --migrations-dir value refuses with exit 2" || bad "missing --migrations-dir value refuses with exit 2 (got $rc)"
single_doc "$OUT/guard-dir.json" \
  && ok "missing-value refusal is one JSON document" \
  || bad "missing-value refusal is one JSON document"
jassert '.verdict == "refused" and .error.code == "invalid-input"' "$OUT/guard-dir.json" \
  "missing --migrations-dir value pins invalid-input"
check_guard() {
  local flag="$1" tag="$2" rc=0
  PATH="$WORK/bin:/usr/bin:/bin" SQUAWK_BIN="" "$GATE" --format json "$flag" \
    >"$OUT/guard-$tag.json" 2>/dev/null || rc=$?
  [[ "$rc" -eq 2 ]] && ok "missing $flag value refuses with exit 2" || bad "missing $flag value refuses with exit 2 (got $rc)"
  jassert '.verdict == "refused" and .error.code == "invalid-input"' "$OUT/guard-$tag.json" \
    "missing $flag value pins invalid-input"
  return 0
}
check_guard --squawk-bin bin
check_guard --squawk-config config

# 9. JSON mode forwards --squawk-config to the linter; "" disables it.
rm -f "$WORK/argv.txt"
rc=0
"$GATE" --format json --squawk-bin "$WORK/bin/squawk-argv" --squawk-config "$WORK/custom.toml" \
  "$WORK/migs/0100_clean.sql" >"$OUT/cfg.json" 2>/dev/null || rc=$?
[[ "$rc" -eq 0 ]] && ok "config-forwarding run exits 0" || bad "config-forwarding run exits 0 (got $rc)"
cfg_line="$(grep -n -x -F -e '--config' "$WORK/argv.txt" | cut -d: -f1 || true)"
path_line="$(grep -n -x -F -e "$WORK/custom.toml" "$WORK/argv.txt" | cut -d: -f1 || true)"
if [[ -n "$cfg_line" && -n "$path_line" && "$cfg_line" -ge 1 ]] && [[ "$path_line" -eq $((cfg_line + 1)) ]]; then
  ok "the configured squawk config reaches the linter argv as --config <path>"
else
  bad "the configured squawk config reaches the linter argv as --config <path>"
fi

rm -f "$WORK/argv.txt"
rc=0
"$GATE" --format json --squawk-bin "$WORK/bin/squawk-argv" --squawk-config "" \
  "$WORK/migs/0100_clean.sql" >"$OUT/nocfg.json" 2>/dev/null || rc=$?
[[ "$rc" -eq 0 ]] && ok "empty-config run exits 0" || bad "empty-config run exits 0 (got $rc)"
if grep -q -F -e '--config' "$WORK/argv.txt"; then
  bad "an empty --squawk-config forwards no --config flag"
else
  ok "an empty --squawk-config forwards no --config flag"
fi

rm -f "$WORK/argv.txt"
rc=0
"$GATE" --format json --squawk-bin "$WORK/bin/squawk-argv" \
  "$WORK/migs/0100_clean.sql" >"$OUT/defcfg.json" 2>/dev/null || rc=$?
[[ "$rc" -eq 0 ]] && ok "default-config run exits 0" || bad "default-config run exits 0 (got $rc)"
if grep -q -F -e '--config' "$WORK/argv.txt" && grep -q -F -e 'squawk-migration.toml' "$WORK/argv.txt"; then
  ok "the default run forwards the canonical squawk config"
else
  bad "the default run forwards the canonical squawk config"
fi

# 10. No captured JSON carries SQL-body bytes or credential prose.
leak=0
for j in "$OUT"/*.json; do
  if grep -q -F -e "$PROBE" -e "$CRED" "$j"; then
    leak=1
    printf '        leak in %s\n' "$j"
  fi
done
[[ "$leak" -eq 0 ]] && ok "no companion-suite JSON leaks body bytes or credential prose" || bad "no companion-suite JSON leaks body bytes or credential prose"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
