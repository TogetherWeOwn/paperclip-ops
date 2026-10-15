#!/usr/bin/env bash
# ===========================================================================
# test_case_a38f4d21_shadow_reader_bounded.sh — offline suite for the case_e6a23bc2
# bounded shadow-reader (ops/case_e6a23bc2/shadow-read-bounded.cjs).
#
# Context: reader request req-20261008T015703Z-2957254 ended
# REFUSED_BOUND_LIMIT (48 retained hourly shards, 236,446,446 bytes vs the
# approved 134,217,728-byte cap). Zero records read is NOT zero matches.
# The corrected reader selects newest-first within the byte budget,
# streams (no full collection), re-verifies the folder binding first, and
# publishes a sanitized receipt only.
#
# What it pins (each with a positive control that proves it can still fail):
#   1. MATCH PREDICATE — a paired-decision-v1 / plugin-shadow / T1 record
#      with explanations[].gate=lane-withdrawn + operand.laneId=cliproxy-meta
#      matches; a host-writer record with the same gate does NOT, a T2
#      record with the same gate does NOT, another lane does NOT.
#   2. PROVEN NEGATIVE — NO_MATCHES exits 0 only with recordsScanned > 0,
#      so a negative is proven work, never an empty read.
#   3. BUDGET WINS — with a small cap, older shards are excluded unread
#      (a planted match there stays unfound by design); aggregateBytes is
#      always within the cap; the receipt names included and excluded.
#   4. FAIL-CLOSED — a single newest shard over budget refuses (exit 2)
#      with no partial read; binding mismatch, empty scope, and unreadable
#      selections refuse (exit 2), never exit 0 with a clean-looking zero.
#   5. NO SYMLINK FOLLOW — a shard-named symlink is skipped and counted,
#      never read, even when it points at a planted match.
#   6. TORN/OVERLONG TOLERANCE — a torn final line and an over-4-MiB line
#      are counted and skipped; the scan still completes.
#   7. SANITIZED RECEIPT — accountKey/authKey fixture values never appear
#      in the receipt; match descriptors carry only ts/tier/pickedModel +
#      gate-operand numbers. --receipt files are 0600.
#   8. SCHEMA/FRESHNESS — other-schema records are counted, never matched;
#      min/max record ts and max age are reported (timestamps examined).
#   9. ZERO-SCANNED REFUSAL — an other-schema-only shard, a shard of only
#      schema-less objects ({} — no schema tag, can never match), or a shard
#      of only valid-JSON scalars/arrays (null, [], 7), refuses with
#      REFUSED_NO_SCANNED_RECORDS (exit 2), never a clean NO_MATCHES;
#      non-object and schema-less lines are counted, never scanned
#      (positive control: one real in-schema record still proves NO_MATCHES
#      with recordsScanned 1).
#
# Hermetic: fixtures in mktemp dirs, pinned --now clock, no network, no
# credentials. Requires node (the reader's runtime).
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
READER="$HERE/ops/case_e6a23bc2/shadow-read-bounded.cjs"
NOW="2026-10-09T18:00:00.000Z"

pass=0
fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
bad() { fail=$((fail + 1)); echo "FAIL - $1"; }

need_node() {
  command -v node >/dev/null 2>&1 || { echo "node required" >&2; exit 2; }
}

# mkshard DIR HOUR [generator-node-expr...]: writes one JSONL shard.
# Generators print records to stdout; use rec() helper below.
mkshard() {
  local dir="$1" hour="$2"
  shift 2
  node -e '
    const rec = (o) => console.log(JSON.stringify(o));
    const base = (over) => Object.assign({
      schema: "paired-decision-v1", writer: "plugin-shadow",
      issueId: "i1", issueIdentifier: "case_8c5fe4cc", ts: "2026-10-09T17:00:00.000Z",
      trigger: "new-card", tier: "T1", pickedModel: "m-x", keptPin: null,
      stateFingerprint: { status: "todo", hadOverride: false, hadRunningRun: false, pinOperator: false },
      laneSnapshot: { ageSeconds: 1, quality: "live", laneFetchErrors: [],
        lanes: { "cliproxy-meta": { weekly: 0.2, fiveHour: 0.01, state: "available",
          paceDeviation: 0, targetBurnRate: null, observedBurnRate: null, deficit: null,
          accounts: [{ accountKey: "SECRET-ACCOUNT-KEY-FIXTURE", authKey: "SECRET-AUTH-KEY-FIXTURE" }] } } },
      candidates: [], explanations: [], explanationsTruncated: 0,
      operatorOverride: null, pickWhy: "fixture",
    }, over);
    const wd = (laneId, util) => ({ modelId: "m-meta", gate: "lane-withdrawn",
      operand: { kind: "lane-withdrawn", laneId, utilization: util, ceiling: 0.98, accounts: 8 } });
    const other = { modelId: "m-y", gate: "tier-floor", operand: { kind: "tier-floor", tier: "T1", requiredTier: "T2" } };
    '"$@"'
  ' > "$dir/decisions-${hour}Z.jsonl"
}

run_reader() { node "$READER" --now "$NOW" "$@"; }

t_match_predicate() {
  local d; d="$(mktemp -d)"; trap 'rm -rf "$d"' RETURN
  mkshard "$d" "2026-10-09-16" 'rec(base({}));'
  mkshard "$d" "2026-10-09-17" '
      rec(base({ ts: "2026-10-09T17:10:00.000Z", explanations: [({ modelId: "m-meta", gate: "lane-withdrawn", operand: { kind: "lane-withdrawn", laneId: "cliproxy-meta", utilization: 0.9812, ceiling: 0.98, accounts: 8 } })] }));
      rec(base({ writer: "host", ts: "2026-10-09T17:11:00.000Z", explanations: [({ modelId: "m-meta", gate: "lane-withdrawn", operand: { kind: "lane-withdrawn", laneId: "cliproxy-meta", utilization: 0.99, ceiling: 0.98, accounts: 8 } })] }));
      rec(base({ tier: "T2", ts: "2026-10-09T17:12:00.000Z", explanations: [({ modelId: "m-meta", gate: "lane-withdrawn", operand: { kind: "lane-withdrawn", laneId: "cliproxy-meta", utilization: 0.99, ceiling: 0.98, accounts: 8 } })] }));
      rec(base({ ts: "2026-10-09T17:13:00.000Z", explanations: [({ modelId: "m-meta", gate: "lane-withdrawn", operand: { kind: "lane-withdrawn", laneId: "cliproxy-other", utilization: 0.99, ceiling: 0.98, accounts: 2 } })] }));
      rec(Object.assign(base({ ts: "2026-10-09T17:14:00.000Z" }), { schema: "legacy-v0" }));
    '
  local out rc
  out="$(run_reader --dir "$d" --expected-realpath "$d" 2>&1)"; rc=$?
  [ $rc -eq 0 ] || { bad "match-predicate: exit $rc, want 0"; return; }
  grep -q '"outcome": "MATCHES_FOUND"' <<<"$out" || { bad "match-predicate: outcome not MATCHES_FOUND"; return; }
  grep -q '"matchesTotal": 1' <<<"$out" || { bad "match-predicate: matchesTotal != 1 (host/T2/other-lane leaked in)"; return; }
  grep -q '"utilization": 0.9812' <<<"$out" || { bad "match-predicate: operand numbers missing"; return; }
  grep -q '"maxRecordTs": "2026-10-09T17:14:00.000Z"' <<<"$out" || { bad "match-predicate: maxRecordTs not examined"; return; }
  grep -q '"legacy-v0": 1' <<<"$out" || { bad "match-predicate: other schema not counted"; return; }
  grep -q 'SECRET-ACCOUNT-KEY-FIXTURE\|SECRET-AUTH-KEY-FIXTURE' <<<"$out" && { bad "match-predicate: secret leaked to receipt"; return; }
  ok "match-predicate: only the T1 plugin-shadow cliproxy-meta record matches, receipt sanitized"
}

t_proven_negative() {
  local d; d="$(mktemp -d)"; trap 'rm -rf "$d"' RETURN
  mkshard "$d" "2026-10-09-17" 'rec(base({ explanations: [({ modelId: "m-y", gate: "tier-floor", operand: { kind: "tier-floor", tier: "T1", requiredTier: "T2" } })] }));'
  local out rc
  out="$(run_reader --dir "$d" --expected-realpath "$d" 2>&1)"; rc=$?
  [ $rc -eq 0 ] || { bad "proven-negative: exit $rc, want 0"; return; }
  grep -q '"outcome": "NO_MATCHES"' <<<"$out" || { bad "proven-negative: outcome not NO_MATCHES"; return; }
  local scanned; scanned="$(echo "$out" | node -e 'let s="";process.stdin.on("data",c=>s+=c).on("end",()=>console.log(JSON.parse(s).recordsScanned))')"
  [ "${scanned:-0}" -gt 0 ] 2>/dev/null || { bad "proven-negative: recordsScanned=$scanned, negative unproven"; return; }
  ok "proven-negative: NO_MATCHES with recordsScanned=$scanned"
}

t_zero_scanned_refuses() {
  local d; d="$(mktemp -d)"; trap 'rm -rf "$d"' RETURN
  # Only an other-schema record (the schema the repo's built plugin emits):
  # nothing in-schema was examined, so a negative would be unproven.
  printf '%s\n' '{"schema":"case_577a5ee0-decision-v1","writer":"plugin-shadow","tier":"T1","ts":"2026-10-09T17:00:00.000Z","explanations":[{"gate":"lane-withdrawn","operand":{"laneId":"cliproxy-meta"}}]}' > "$d/decisions-2026-10-09-17Z.jsonl"
  local out rc
  out="$(run_reader --dir "$d" --expected-realpath "$d" 2>&1)"; rc=$?
  { [ $rc -eq 2 ] && grep -q 'REFUSED_NO_SCANNED_RECORDS' <<<"$out"; } || { bad "zero-scanned: other-schema-only shard did not refuse (exit $rc)"; return; }
  grep -q '"outcome": "NO_MATCHES"' <<<"$out" && { bad "zero-scanned: unproven negative published"; return; }
  # Valid-JSON non-records never count as scanned.
  local e; e="$(mktemp -d)"; trap 'rm -rf "$d" "$e"' RETURN
  printf '%s\n' 'null' '[]' '7' > "$e/decisions-2026-10-09-17Z.jsonl"
  out="$(run_reader --dir "$e" --expected-realpath "$e" 2>&1)"; rc=$?
  { [ $rc -eq 2 ] && grep -q 'REFUSED_NO_SCANNED_RECORDS' <<<"$out"; } || { bad "zero-scanned: scalar/array lines counted as scanned (exit $rc)"; return; }
  grep -q '"nonObjectLines": 3' <<<"$out" || { bad "zero-scanned: non-object lines not counted"; return; }
  # Positive control: one real in-schema non-match still proves the negative.
  mkshard "$e" "2026-10-09-18" 'rec(base({}));'
  out="$(run_reader --dir "$e" --expected-realpath "$e" 2>&1)"; rc=$?
  [ $rc -eq 0 ] || { bad "zero-scanned: proven negative exit $rc, want 0"; return; }
  grep -q '"outcome": "NO_MATCHES"' <<<"$out" || { bad "zero-scanned: proven negative not NO_MATCHES"; return; }
  grep -q '"recordsScanned": 1' <<<"$out" || { bad "zero-scanned: recordsScanned != 1 (non-objects leaked in)"; return; }
  ok "zero-scanned: other-schema-only and scalar-only shards refuse (exit 2); one real record proves NO_MATCHES"
}

t_schema_less_refuses() {
  local d; d="$(mktemp -d)"; trap 'rm -rf "$d"' RETURN
  # Schema-less objects ({}) carry no schema tag, so they can never match.
  # Counting them as scanned would publish a proven-looking NO_MATCHES over
  # zero evidence — they must refuse instead.
  printf '%s\n' '{}' '{"ts":"2026-10-09T17:00:00.000Z"}' > "$d/decisions-2026-10-09-17Z.jsonl"
  local out rc
  out="$(run_reader --dir "$d" --expected-realpath "$d" 2>&1)"; rc=$?
  { [ $rc -eq 2 ] && grep -q 'REFUSED_NO_SCANNED_RECORDS' <<<"$out"; } || { bad "schema-less: {}-only shard did not refuse (exit $rc)"; return; }
  grep -q '"outcome": "NO_MATCHES"' <<<"$out" && { bad "schema-less: unproven negative published"; return; }
  grep -q '"schemaLessLines": 2' <<<"$out" || { bad "schema-less: schema-less lines not counted"; return; }
  # Positive control: one real in-schema non-match still proves the negative,
  # and the schema-less lines stay out of the scanned count.
  mkshard "$d" "2026-10-09-18" 'rec(base({}));'
  out="$(run_reader --dir "$d" --expected-realpath "$d" 2>&1)"; rc=$?
  [ $rc -eq 0 ] || { bad "schema-less: proven negative exit $rc, want 0"; return; }
  grep -q '"outcome": "NO_MATCHES"' <<<"$out" || { bad "schema-less: proven negative not NO_MATCHES"; return; }
  grep -q '"recordsScanned": 1' <<<"$out" || { bad "schema-less: recordsScanned != 1 (schema-less lines leaked in)"; return; }
  ok "schema-less: {}-only shard refuses (exit 2); one real record proves NO_MATCHES"
}

t_budget_wins() {
  local d; d="$(mktemp -d)"; trap 'rm -rf "$d"' RETURN
  # Old shard carries the only match; small cap forces its exclusion.
  mkshard "$d" "2026-10-09-15" 'rec(base({ ts: "2026-10-09T15:10:00.000Z", explanations: [({ modelId: "m-meta", gate: "lane-withdrawn", operand: { kind: "lane-withdrawn", laneId: "cliproxy-meta", utilization: 0.99, ceiling: 0.98, accounts: 8 } })] }));'
  mkshard "$d" "2026-10-09-16" 'rec(base({}));'
  mkshard "$d" "2026-10-09-17" 'rec(base({}));'
  local newest_size; newest_size="$(stat -c %s "$d/decisions-2026-10-09-17Z.jsonl")"
  local mid_size; mid_size="$(stat -c %s "$d/decisions-2026-10-09-16Z.jsonl")"
  local cap=$((newest_size + mid_size)) # fits 17+16, excludes 15 by design
  local out rc
  out="$(run_reader --dir "$d" --expected-realpath "$d" --cap-bytes "$cap" 2>&1)"; rc=$?
  [ $rc -eq 0 ] || { bad "budget-wins: exit $rc, want 0"; return; }
  grep -q '"outcome": "NO_MATCHES"' <<<"$out" || { bad "budget-wins: excluded match leaked in"; return; }
  grep -q 'decisions-2026-10-09-15' <<<"$out" || { bad "budget-wins: excluded shard unnamed"; return; }
  local agg; agg="$(echo "$out" | node -e 'let s="";process.stdin.on("data",c=>s+=c).on("end",()=>console.log(JSON.parse(s).aggregateBytes))')"
  [ "${agg:-999999999}" -le "$cap" ] 2>/dev/null || { bad "budget-wins: aggregate $agg over cap $cap"; return; }
  ok "budget-wins: newest-first within cap, older match-shard excluded unread (agg=$agg<=cap=$cap)"
}

t_single_shard_over_budget() {
  local d; d="$(mktemp -d)"; trap 'rm -rf "$d"' RETURN
  mkshard "$d" "2026-10-09-17" 'rec(base({}));'
  local out rc
  out="$(run_reader --dir "$d" --expected-realpath "$d" --cap-bytes 10 2>&1)"; rc=$?
  [ $rc -eq 2 ] || { bad "over-budget: exit $rc, want 2 (fail-closed)"; return; }
  grep -q 'REFUSED_SINGLE_SHARD_OVER_BUDGET' <<<"$out" || { bad "over-budget: wrong refusal"; return; }
  grep -q 'recordsScanned' <<<"$out" && { bad "over-budget: partial read claimed"; return; }
  ok "over-budget: single newest shard over cap refuses with zero records read"
}

t_binding_and_scope_refusals() {
  local d; d="$(mktemp -d)"; trap 'rm -rf "$d"' RETURN
  mkshard "$d" "2026-10-09-17" 'rec(base({}));'
  local out rc
  out="$(run_reader --dir "$d" --expected-realpath /nonexistent-binding 2>&1)"; rc=$?
  { [ $rc -eq 2 ] && grep -q 'REFUSED_BINDING' <<<"$out"; } || { bad "binding: mismatch did not refuse"; return; }
  local e; e="$(mktemp -d)"; trap 'rm -rf "$d" "$e"' RETURN
  out="$(run_reader --dir "$e" --expected-realpath "$e" 2>&1)"; rc=$?
  { [ $rc -eq 2 ] && grep -q 'REFUSED_NO_SHARDS' <<<"$out"; } || { bad "scope: empty dir did not refuse (must differ from NO_MATCHES)"; return; }
  out="$(node "$READER" 2>&1)"; rc=$?
  [ $rc -eq 1 ] || { bad "usage: no-args exit $rc, want 1"; return; }
  ok "binding/scope/usage: mismatched binding, empty scope refuse (exit 2); usage exits 1"
}

t_symlink_never_followed() {
  local d outside; d="$(mktemp -d)"; outside="$(mktemp -d)"; trap 'rm -rf "$d" "$outside"' RETURN
  mkshard "$outside" "2026-10-09-17" 'rec(base({ ts: "2026-10-09T17:10:00.000Z", explanations: [({ modelId: "m-meta", gate: "lane-withdrawn", operand: { kind: "lane-withdrawn", laneId: "cliproxy-meta", utilization: 0.99, ceiling: 0.98, accounts: 8 } })] }));'
  mv "$outside/decisions-2026-10-09-17Z.jsonl" "$outside/target.jsonl"
  ln -s "$outside/target.jsonl" "$d/decisions-2026-10-09-17Z.jsonl"
  mkshard "$d" "2026-10-09-16" 'rec(base({}));'
  local out rc
  out="$(run_reader --dir "$d" --expected-realpath "$d" 2>&1)"; rc=$?
  [ $rc -eq 0 ] || { bad "symlink: exit $rc, want 0"; return; }
  grep -q '"outcome": "NO_MATCHES"' <<<"$out" || { bad "symlink: planted match was followed"; return; }
  grep -q '"symlinksSkipped": 1' <<<"$out" || { bad "symlink: skip not counted"; return; }
  ok "symlink: shard-named symlink skipped, counted, never followed"
}

t_torn_and_overlong() {
  local d; d="$(mktemp -d)"; trap 'rm -rf "$d"' RETURN
  mkshard "$d" "2026-10-09-17" 'rec(base({}));'
  printf '{"schema":"paired-decision-v1","writer":"plugin-shadow","tier":\n' >> "$d/decisions-2026-10-09-17Z.jsonl" # torn tail: own line, unparseable
  node -e 'process.stdout.write("x".repeat(5*1024*1024) + "\n")' >> "$d/decisions-2026-10-09-17Z.jsonl"
  local out rc
  out="$(run_reader --dir "$d" --expected-realpath "$d" 2>&1)"; rc=$?
  [ $rc -eq 0 ] || { bad "torn: exit $rc, want 0"; return; }
  grep -q '"malformedLines": 1' <<<"$out" || { bad "torn: torn tail not counted"; return; }
  grep -q '"overlongLinesSkipped": 1' <<<"$out" || { bad "torn: overlong line not counted"; return; }
  ok "torn/overlong: counted, skipped, scan completes"
}

t_receipt_file_mode() {
  local d; d="$(mktemp -d)"; trap 'rm -rf "$d"' RETURN
  mkshard "$d" "2026-10-09-17" 'rec(base({}));'
  run_reader --dir "$d" --expected-realpath "$d" --receipt "$d/r.json" >/dev/null 2>&1 || { bad "receipt: reader failed"; return; }
  [ "$(stat -c %a "$d/r.json")" = "600" ] || { bad "receipt: mode $(stat -c %a "$d/r.json"), want 600"; return; }
  grep -q '"outcome": "NO_MATCHES"' "$d/r.json" || { bad "receipt: file content wrong"; return; }
  ok "receipt: --receipt file written 0600 with the verdict"
}

need_node
[ -f "$READER" ] || { echo "reader missing: $READER" >&2; exit 2; }
t_match_predicate
t_proven_negative
t_zero_scanned_refuses
t_schema_less_refuses
t_budget_wins
t_single_shard_over_budget
t_binding_and_scope_refusals
t_symlink_never_followed
t_torn_and_overlong
t_receipt_file_mode

echo "---"
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
