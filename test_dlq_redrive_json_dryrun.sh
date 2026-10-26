#!/usr/bin/env bash
# ===========================================================================
# test_dlq_redrive_json_dryrun.sh — acceptance suite for JSON verdicts and
# dry-run previews on the standalone DLQ redrive CLI.
#
# IMMUTABLE ACCEPTANCE ARTIFACT. The Builder implements the feature on this
# branch and never edits this file. Baseline on the base commit is RED: the
# tool accepts no --json or --dry-run flag, so every feature section below
# fails with bad_usage while section 9 (positive controls, no new flags)
# stays GREEN.
#
# WHAT THIS SUITE PINS (consumer: AI delivery agents rehearsing offline
# fixture mutations as data instead of scraping TSV):
#
#  * LIST --json. Exactly one JSON document on stdout carrying a numeric
#    schema_version (currently 1) and an entries array. Each entry carries
#    id, status, attempts, topic, failed_at and error. TSV default output
#    stays byte-identical; JSON entries equal TSV rows (same ids, same
#    order, same count) with and without --status/--topic filters.
#
#  * REDRIVE/PURGE --json. Exactly one JSON document with schema_version
#    and an outcomes array; each outcome carries the new status, the id
#    and the reason. Real mutations still rewrite the fixture and append
#    one journal record per entry.
#
#  * --dry-run (redrive/purge). Identical selection, validation and
#    reason-registry logic, but writes nothing: fixture bytes identical
#    (sha256 before/after), journal absent-or-identical, no lock file and
#    no stray mutate/entries temp files. Per-entry outcomes are marked
#    preview=true (real runs never report preview=true). Zero matches
#    still exit 3; refused reasons still exit 2.
#
#  * REFUSALS UNDER --json. One parseable envelope (single JSON document)
#    carrying the EXISTING stable cause (already_purged, no_match,
#    reason_not_for_action, unknown_reason, malformed_fixture,
#    fixture_unreadable, ...) plus a non-empty next_action. Exit codes
#    are unchanged: 0 ok, 2 refused, 3 not eligible, 5 unmeasured.
#    Refusal envelopes never echo fixture payload bytes.
#
#  * EXIT-CODE NOTE. The planning note lists "malformed fixture exit 5";
#    the tool contract (and the existing suite) is malformed -> exit 2
#    (refused) and missing/unreadable fixture -> exit 5 (unmeasured).
#    This suite pins both correctly: malformed_fixture at 2 and
#    fixture_unreadable at 5, each under --json with a JSON envelope.
#
# No broker, no database, no network, no credentials: every fixture is
# fabricated per-section in a mktemp tree, and DLQ_FIXTURE points the
# tool at it.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$HERE/dlq_redrive.sh"
PASS=0; FAIL=0

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export DLQ_ACTOR="suite-json"

ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

# assert_rc <label> <expected_exit> <actual_exit> [stdout]
assert_rc() {
  local label="$1" xrc="$2" rc="$3" out="${4:-}"
  if [[ "$rc" == "$xrc" ]]; then ok "$label"
  else bad "$label" "expected exit $xrc, got $rc${out:+; out: $out}"; fi
}

# assert_single_json <label> <stdout>: stdout is exactly one JSON document.
assert_single_json() {
  local label="$1" out="$2"
  if jq -s -e 'length == 1' <<<"$out" >/dev/null 2>&1; then ok "$label"
  else bad "$label" "expected exactly one JSON document on stdout; got: $out"; fi
}

# json_field <stdout> <jq-filter>: raw value or empty on failure.
json_field() {
  local out="$1" filter="$2"
  jq -r "$filter // empty" <<<"$out" 2>/dev/null || true
}

# assert_schema_version <label> <stdout>
assert_schema_version() {
  local label="$1" out="$2"
  if jq -e '.schema_version == 1' <<<"$out" >/dev/null 2>&1; then ok "$label"
  else bad "$label" "expected .schema_version == 1 (JSON number); got: $out"; fi
}

# assert_json_cause <label> <expected_exit> <expected_cause> <stdout> <actual_exit>
# Refusal envelope carries the existing stable cause plus a non-empty
# next_action, either nested under .error or top-level.
assert_json_cause() {
  local label="$1" xrc="$2" xc="$3" out="$4" rc="$5"
  local cause next single="no"
  cause="$(json_field "$out" '.error.cause // .cause')"
  next="$(json_field "$out" '.error.next_action // .next_action')"
  jq -s -e 'length == 1' <<<"$out" >/dev/null 2>&1 && single="yes"
  if [[ "$rc" == "$xrc" && "$cause" == "$xc" && -n "$next" && "$single" == "yes" ]]; then
    ok "$label"
  else
    bad "$label" "expected rc=$xrc cause=$xc single-json next_action nonempty; got rc=$rc cause=$cause next_action=$next single=$single; out: $out"
  fi
}

# file_hash <path>: sha256 hex or ABSENT when missing.
file_hash() {
  if [[ -f "$1" ]]; then sha256sum "$1" | cut -d' ' -f1; else printf 'ABSENT'; fi
}

# assert_no_strays <label> <fixture>: no lock file and no mutate/entries
# temp files beside the fixture.
assert_no_strays() {
  local label="$1" f="$2" dir strays
  dir="$(dirname "$f")"
  strays="$(ls "$f.lock" "$dir"/.dlq_mutate.* "$dir"/.dlq_entries.* 2>/dev/null || true)"
  if [[ -z "$strays" ]]; then ok "$label"
  else bad "$label" "stray files beside fixture: $strays"; fi
}

# mkfixture <name> <line...>: writes lines to $WORK/<name>.jsonl, prints path.
mkfixture() {
  local name="$1"; shift
  local f="$WORK/$name.jsonl"
  : > "$f"
  local line
  for line in "$@"; do printf '%s\n' "$line" >> "$f"; done
  printf '%s' "$f"
}

E1='{"id":"m1","topic":"billing.invoiced","payload":{"a":1},"error":"timeout","failed_at":"2026-09-20T04:00:00Z","attempts":5,"status":"dead"}'
E2='{"id":"m2","topic":"mail.welcome","payload":{},"error":"conn-refused","failed_at":"2026-09-21T04:00:00Z","attempts":9,"status":"dead"}'
E3='{"id":"m3","topic":"billing.refund","payload":{},"error":"timeout","failed_at":"2026-09-01T00:00:00Z","attempts":2,"status":"dead"}'
MARKED='{"id":"mx","topic":"billing.marked","payload":{"note":"PAYLOAD_MARKER_7Q2Z"},"error":"timeout","failed_at":"2026-09-20T04:00:00Z","attempts":1,"status":"dead"}'

echo "== 1. list --json: shape and TSV parity =="

F="$(mkfixture listjson "$E1" "$E2" "")"
tsv="$(DLQ_FIXTURE="$F" "$TOOL" list 2>/dev/null)"; tsv_rc=$?
out="$(DLQ_FIXTURE="$F" "$TOOL" list --json 2>/dev/null)"; rc=$?
assert_rc "list --json exits 0 on a good fixture" 0 "$rc" "$out"
assert_single_json "list --json prints exactly one JSON document" "$out"
assert_schema_version "list --json carries schema_version 1" "$out"
[[ "$(json_field "$out" '.entries | length')" == "2" ]] \
  && ok "list --json entries array holds one element per entry" \
  || bad "list --json entries array holds one element per entry" "got: $out"
[[ "$(json_field "$out" '.entries[0].id')" == "m1" \
  && "$(json_field "$out" '.entries[0].status')" == "dead" \
  && "$(json_field "$out" '.entries[0].attempts')" == "5" \
  && "$(json_field "$out" '.entries[0].topic')" == "billing.invoiced" \
  && "$(json_field "$out" '.entries[0].failed_at')" == "2026-09-20T04:00:00Z" \
  && "$(json_field "$out" '.entries[0].error')" == "timeout" ]] \
  && ok "list --json entry carries id/status/attempts/topic/failed_at/error" \
  || bad "list --json entry carries id/status/attempts/topic/failed_at/error" "got: $out"
tsv_ids="$(cut -f1 <<<"$tsv")"
json_ids="$(json_field "$out" '[.entries[].id] | join("\n")')"
[[ "$tsv_rc" == "0" && -n "$tsv_ids" && "$tsv_ids" == "$json_ids" ]] \
  && ok "list JSON ids match TSV ids in order (parity)" \
  || bad "list JSON ids match TSV ids in order (parity)" "tsv_rc=$tsv_rc tsv: $tsv_ids json: $json_ids"

echo "== 2. list --json with status/topic filters =="

F="$(mkfixture listfilter "$E1" "$E2" "$E3")"
DLQ_FIXTURE="$F" "$TOOL" redrive --id m1 --reason transient_upstream >/dev/null 2>&1
tsv="$(DLQ_FIXTURE="$F" "$TOOL" list --status redriven 2>/dev/null)"
out="$(DLQ_FIXTURE="$F" "$TOOL" list --status redriven --json 2>/dev/null)"; rc=$?
assert_rc "list --status redriven --json exits 0" 0 "$rc" "$out"
assert_single_json "filtered list --json is one JSON document" "$out"
[[ "$(json_field "$out" '.entries | length')" == "1" \
  && "$(json_field "$out" '.entries[0].id')" == "m1" \
  && "$(json_field "$out" '.entries[0].status')" == "redriven" ]] \
  && ok "list --status filter applies under --json" \
  || bad "list --status filter applies under --json" "got: $out"
[[ "$(cut -f1 <<<"$tsv")" == "$(json_field "$out" '[.entries[].id] | join("\n")')" ]] \
  && ok "filtered list JSON/TSV parity (status)" \
  || bad "filtered list JSON/TSV parity (status)" "tsv: $tsv json: $out"
tsv="$(DLQ_FIXTURE="$F" "$TOOL" list --topic mail.welcome 2>/dev/null)"
out="$(DLQ_FIXTURE="$F" "$TOOL" list --topic mail.welcome --json 2>/dev/null)"; rc=$?
assert_rc "list --topic --json exits 0" 0 "$rc" "$out"
[[ "$(json_field "$out" '.entries | length')" == "1" \
  && "$(json_field "$out" '.entries[0].id')" == "m2" ]] \
  && ok "list --topic filter applies under --json" \
  || bad "list --topic filter applies under --json" "got: $out"
[[ "$(cut -f1 <<<"$tsv")" == "$(json_field "$out" '[.entries[].id] | join("\n")')" ]] \
  && ok "filtered list JSON/TSV parity (topic)" \
  || bad "filtered list JSON/TSV parity (topic)" "tsv: $tsv json: $out"

echo "== 3. redrive/purge --json: per-entry outcomes =="

F="$(mkfixture mutjson "$E1" "$E2" "$E3")"
G="$(mkfixture muttsv "$E1" "$E2" "$E3")"
tsv="$(DLQ_FIXTURE="$G" "$TOOL" redrive --id m1 --reason transient_upstream 2>/dev/null)"
out="$(DLQ_FIXTURE="$F" "$TOOL" redrive --id m1 --reason transient_upstream --json 2>/dev/null)"; rc=$?
assert_rc "redrive --id --json exits 0" 0 "$rc" "$out"
assert_single_json "redrive --json prints exactly one JSON document" "$out"
assert_schema_version "redrive --json carries schema_version 1" "$out"
[[ "$(json_field "$out" '.outcomes | length')" == "1" \
  && "$(json_field "$out" '.outcomes[0].id')" == "m1" \
  && "$(json_field "$out" '.outcomes[0].status')" == "redriven" \
  && "$(json_field "$out" '.outcomes[0].reason')" == "transient_upstream" ]] \
  && ok "redrive --json outcome carries new-status/id/reason" \
  || bad "redrive --json outcome carries new-status/id/reason" "got: $out"
[[ "$(json_field "$out" '.outcomes[0].preview')" != "true" ]] \
  && ok "real redrive --json never reports preview=true" \
  || bad "real redrive --json never reports preview=true" "got: $out"
[[ "$(head -n1 <<<"$tsv")" == $'redriven\tm1\ttransient_upstream' \
  && "$(json_field "$out" '.outcomes[0].status')" == "$(cut -f1 <<<"$tsv")" \
  && "$(json_field "$out" '.outcomes[0].id')" == "$(cut -f2 <<<"$tsv")" \
  && "$(json_field "$out" '.outcomes[0].reason')" == "$(cut -f3 <<<"$tsv")" ]] \
  && ok "redrive JSON/TSV verdict parity on twin fixtures" \
  || bad "redrive JSON/TSV verdict parity on twin fixtures" "tsv: $tsv json: $out"
grep -q '"id":"m1".*"status":"redriven".*"redrive_reason":"transient_upstream"' "$F" \
  && ok "redrive --json still rewrites the fixture entry" \
  || bad "redrive --json still rewrites the fixture entry" "$(grep m1 "$F")"
[[ -s "$F.journal.jsonl" ]] && grep -q '"action":"redrive".*"id":"m1".*"reason":"transient_upstream"' "$F.journal.jsonl" \
  && ok "redrive --json still appends one journal record" \
  || bad "redrive --json still appends one journal record" "$(cat "$F.journal.jsonl" 2>/dev/null)"

F="$(mkfixture purgejson "$E1" "$E2")"
out="$(DLQ_FIXTURE="$F" "$TOOL" purge --id m2 --reason duplicate --json 2>/dev/null)"; rc=$?
assert_rc "purge --id --json exits 0" 0 "$rc" "$out"
assert_single_json "purge --json prints exactly one JSON document" "$out"
[[ "$(json_field "$out" '.outcomes[0].id')" == "m2" \
  && "$(json_field "$out" '.outcomes[0].status')" == "purged" \
  && "$(json_field "$out" '.outcomes[0].reason')" == "duplicate" ]] \
  && ok "purge --json outcome carries new-status/id/reason" \
  || bad "purge --json outcome carries new-status/id/reason" "got: $out"
grep -q '"id":"m2".*"status":"purged".*"purge_reason":"duplicate"' "$F" \
  && grep -q '"action":"purge".*"id":"m2"' "$F.journal.jsonl" \
  && ok "purge --json still rewrites fixture and journal" \
  || bad "purge --json still rewrites fixture and journal" "$(cat "$F" "$F.journal.jsonl" 2>/dev/null)"

echo "== 4. dry-run previews write nothing =="

F="$(mkfixture dryredrive "$E1" "$E2" "$E3")"
before_fixture="$(file_hash "$F")"
before_journal="$(file_hash "$F.journal.jsonl")"
out="$(DLQ_FIXTURE="$F" "$TOOL" redrive --id m1 --reason transient_upstream --dry-run --json 2>/dev/null)"; rc=$?
assert_rc "dry-run redrive --id exits 0" 0 "$rc" "$out"
assert_single_json "dry-run redrive prints exactly one JSON document" "$out"
[[ "$(json_field "$out" '.outcomes | length')" == "1" \
  && "$(json_field "$out" '.outcomes[0].id')" == "m1" \
  && "$(json_field "$out" '.outcomes[0].status')" == "redriven" \
  && "$(json_field "$out" '.outcomes[0].reason')" == "transient_upstream" \
  && "$(json_field "$out" '.outcomes[0].preview')" == "true" ]] \
  && ok "dry-run outcome carries status/id/reason with preview=true" \
  || bad "dry-run outcome carries status/id/reason with preview=true" "got: $out"
[[ "$(file_hash "$F")" == "$before_fixture" ]] \
  && ok "dry-run leaves the fixture byte-identical (sha256)" \
  || bad "dry-run leaves the fixture byte-identical (sha256)" "$(cat "$F")"
[[ "$(file_hash "$F.journal.jsonl")" == "$before_journal" && "$before_journal" == "ABSENT" ]] \
  && ok "dry-run appends no journal records (journal stays absent)" \
  || bad "dry-run appends no journal records (journal stays absent)" "$(cat "$F.journal.jsonl" 2>/dev/null)"
assert_no_strays "dry-run creates no lock or temp files" "$F"

F="$(mkfixture drybulk "$E1" "$E2" "$E3")"
DLQ_FIXTURE="$F" "$TOOL" redrive --id m1 --reason transient_upstream >/dev/null 2>&1
# Setup above is a REAL mutation, which owns its lock file. Clear it so the
# stray check below measures only what the dry-run itself creates (nothing).
rm -f "$F.lock"
before_fixture="$(file_hash "$F")"
before_journal="$(file_hash "$F.journal.jsonl")"
out="$(DLQ_FIXTURE="$F" "$TOOL" purge --all --reason expired_stale --dry-run --json 2>/dev/null)"; rc=$?
assert_rc "dry-run bulk purge exits 0" 0 "$rc" "$out"
[[ "$(json_field "$out" '.outcomes | length')" == "2" ]] \
  && ok "dry-run bulk purge previews exactly the dead entries" \
  || bad "dry-run bulk purge previews exactly the dead entries" "got: $out"
[[ "$(json_field "$out" '[.outcomes[].preview] | all')" == "true" ]] \
  && ok "every dry-run bulk outcome is marked preview" \
  || bad "every dry-run bulk outcome is marked preview" "got: $out"
[[ "$(file_hash "$F")" == "$before_fixture" ]] \
  && ok "dry-run bulk leaves fixture bytes identical" \
  || bad "dry-run bulk leaves fixture bytes identical" "$(cat "$F")"
[[ "$(file_hash "$F.journal.jsonl")" == "$before_journal" ]] \
  && ok "dry-run bulk leaves a pre-existing journal identical" \
  || bad "dry-run bulk leaves a pre-existing journal identical" "$(cat "$F.journal.jsonl" 2>/dev/null)"
assert_no_strays "dry-run bulk creates no lock or temp files" "$F"
[[ "$(wc -l < "$F")" == "3" ]] \
  && ok "dry-run bulk drops no rows" \
  || bad "dry-run bulk drops no rows" "$(cat "$F")"

echo "== 5. already_purged and zero-match under JSON/dry-run =="

F="$(mkfixture purgedterm "$E1" "$E2")"
DLQ_FIXTURE="$F" "$TOOL" purge --id m1 --reason duplicate >/dev/null 2>&1
before_fixture="$(file_hash "$F")"
before_journal="$(file_hash "$F.journal.jsonl")"
out="$(DLQ_FIXTURE="$F" "$TOOL" redrive --id m1 --reason transient_upstream --json 2>/dev/null)"; rc=$?
assert_json_cause "redrive of purged id under --json is exit 3 already_purged" 3 already_purged "$out" "$rc"
out="$(DLQ_FIXTURE="$F" "$TOOL" redrive --id m1 --reason transient_upstream --dry-run --json 2>/dev/null)"; rc=$?
assert_json_cause "dry-run redrive of purged id is exit 3 already_purged" 3 already_purged "$out" "$rc"
[[ "$(file_hash "$F")" == "$before_fixture" ]] \
  && ok "already_purged refusals leave the fixture untouched" \
  || bad "already_purged refusals leave the fixture untouched" "$(cat "$F")"
[[ "$(file_hash "$F.journal.jsonl")" == "$before_journal" ]] \
  && ok "already_purged refusals append no journal records" \
  || bad "already_purged refusals append no journal records" "$(cat "$F.journal.jsonl" 2>/dev/null)"

F="$(mkfixture zeromatch "$E1" "$E2")"
out="$(DLQ_FIXTURE="$F" "$TOOL" redrive --all --topic no.such.topic --reason transient_upstream --json 2>/dev/null)"; rc=$?
assert_json_cause "JSON --all zero-match is exit 3 no_match" 3 no_match "$out" "$rc"
out="$(DLQ_FIXTURE="$F" "$TOOL" purge --all --topic no.such.topic --reason expired_stale --dry-run --json 2>/dev/null)"; rc=$?
assert_json_cause "dry-run --all zero-match is exit 3 no_match" 3 no_match "$out" "$rc"

echo "== 6. reason-registry refusals under --json =="

F="$(mkfixture jsonreasons "$E1")"
before_fixture="$(file_hash "$F")"
out="$(DLQ_FIXTURE="$F" "$TOOL" redrive --id m1 --reason duplicate --json 2>/dev/null)"; rc=$?
assert_json_cause "duplicate as redrive reason is exit 2 reason_not_for_action" 2 reason_not_for_action "$out" "$rc"
out="$(DLQ_FIXTURE="$F" "$TOOL" redrive --id m1 --reason whatever --json 2>/dev/null)"; rc=$?
assert_json_cause "unknown redrive reason is exit 2 unknown_reason" 2 unknown_reason "$out" "$rc"
out="$(DLQ_FIXTURE="$F" "$TOOL" purge --id m1 --reason transient_upstream --dry-run --json 2>/dev/null)"; rc=$?
assert_json_cause "redrive reason on dry-run purge is exit 2 reason_not_for_action" 2 reason_not_for_action "$out" "$rc"
[[ "$(file_hash "$F")" == "$before_fixture" ]] \
  && ok "JSON reason refusals leave the fixture untouched" \
  || bad "JSON reason refusals leave the fixture untouched" "$(cat "$F")"
[[ ! -e "$F.journal.jsonl" ]] \
  && ok "JSON reason refusals append no journal records" \
  || bad "JSON reason refusals append no journal records" "$(cat "$F.journal.jsonl" 2>/dev/null)"

echo "== 7. malformed and missing fixtures under --json =="

F="$(mkfixture jsonbad "$E1" 'this is not json')"
before_fixture="$(file_hash "$F")"
out="$(DLQ_FIXTURE="$F" "$TOOL" list --json 2>/dev/null)"; rc=$?
assert_json_cause "malformed fixture under --json is exit 2 malformed_fixture" 2 malformed_fixture "$out" "$rc"
out="$(DLQ_FIXTURE="$F" "$TOOL" redrive --all --reason transient_upstream --dry-run --json 2>/dev/null)"; rc=$?
assert_json_cause "dry-run on a malformed fixture is exit 2 malformed_fixture" 2 malformed_fixture "$out" "$rc"
[[ "$(file_hash "$F")" == "$before_fixture" ]] \
  && ok "malformed fixture stays byte-identical under --json" \
  || bad "malformed fixture stays byte-identical under --json" "$(cat "$F")"
[[ ! -e "$F.journal.jsonl" ]] \
  && ok "malformed refusals create no journal file" \
  || bad "malformed refusals create no journal file" "$(cat "$F.journal.jsonl" 2>/dev/null)"

out="$(DLQ_FIXTURE="$WORK/does-not-exist.jsonl" "$TOOL" list --json 2>/dev/null)"; rc=$?
assert_json_cause "missing fixture under --json is exit 5 fixture_unreadable" 5 fixture_unreadable "$out" "$rc"
out="$(DLQ_FIXTURE="$WORK/does-not-exist.jsonl" "$TOOL" purge --all --reason expired_stale --dry-run --json 2>/dev/null)"; rc=$?
assert_json_cause "dry-run against a missing fixture is exit 5 fixture_unreadable" 5 fixture_unreadable "$out" "$rc"

echo "== 8. refusal envelopes never echo payload bytes =="

F="$(mkfixture nopayload "$MARKED")"
out="$(DLQ_FIXTURE="$F" "$TOOL" redrive --id mx --reason whatever --json 2>/dev/null)"; rc=$?
assert_json_cause "marked refusal is a JSON unknown_reason envelope" 2 unknown_reason "$out" "$rc"
[[ "$out" != *"PAYLOAD_MARKER_7Q2Z"* ]] \
  && ok "unknown_reason envelope carries no payload bytes" \
  || bad "unknown_reason envelope carries no payload bytes" "got: $out"
DLQ_FIXTURE="$F" "$TOOL" purge --id mx --reason duplicate >/dev/null 2>&1
out="$(DLQ_FIXTURE="$F" "$TOOL" redrive --id mx --reason transient_upstream --json 2>/dev/null)"; rc=$?
assert_json_cause "marked already_purged is a JSON envelope" 3 already_purged "$out" "$rc"
[[ "$out" != *"PAYLOAD_MARKER_7Q2Z"* ]] \
  && ok "already_purged envelope carries no payload bytes" \
  || bad "already_purged envelope carries no payload bytes" "got: $out"

echo "== 9. positive controls: existing TSV behavior without new flags =="

F="$(mkfixture control "$E1" "$E2" "")"
out="$(DLQ_FIXTURE="$F" "$TOOL" list 2>/dev/null)"; rc=$?
assert_rc "control: list TSV exits 0" 0 "$rc" "$out"
[[ "$(printf '%s\n' "$out" | wc -l)" == "2" ]] && ok "control: list TSV renders one line per entry" \
  || bad "control: list TSV renders one line per entry" "got: $out"
out="$(DLQ_FIXTURE="$F" "$TOOL" redrive --id m1 --reason transient_upstream 2>/dev/null)"; rc=$?
[[ "$rc" == "0" && "$(head -n1 <<<"$out")" == $'redriven\tm1\ttransient_upstream' ]] \
  && ok "control: redrive TSV reports status/id/reason at exit 0" \
  || bad "control: redrive TSV reports status/id/reason at exit 0" "rc=$rc out: $out"
out="$(DLQ_FIXTURE="$F" "$TOOL" purge --id m2 --reason duplicate 2>/dev/null)"; rc=$?
[[ "$rc" == "0" && "$(head -n1 <<<"$out")" == $'purged\tm2\tduplicate' ]] \
  && ok "control: purge TSV reports status/id/reason at exit 0" \
  || bad "control: purge TSV reports status/id/reason at exit 0" "rc=$rc out: $out"
out="$(DLQ_FIXTURE="$F" "$TOOL" redrive --id m2 --reason transient_upstream 2>/dev/null)"; rc=$?
[[ "$rc" == "3" && "$(head -n1 <<<"$out" | cut -f2)" == "already_purged" ]] \
  && ok "control: redrive of purged id stays exit 3 already_purged" \
  || bad "control: redrive of purged id stays exit 3 already_purged" "rc=$rc out: $out"

G="$(mkfixture controlbad "$E1" 'this is not json')"
out="$(DLQ_FIXTURE="$G" "$TOOL" list 2>/dev/null)"; rc=$?
[[ "$rc" == "2" && "$(head -n1 <<<"$out" | cut -f2)" == "malformed_fixture" ]] \
  && ok "control: malformed fixture stays exit 2 malformed_fixture" \
  || bad "control: malformed fixture stays exit 2 malformed_fixture" "rc=$rc out: $out"
out="$(DLQ_FIXTURE="$WORK/does-not-exist.jsonl" "$TOOL" list 2>/dev/null)"; rc=$?
[[ "$rc" == "5" && "$(head -n1 <<<"$out" | cut -f2)" == "fixture_unreadable" ]] \
  && ok "control: missing fixture stays exit 5 fixture_unreadable" \
  || bad "control: missing fixture stays exit 5 fixture_unreadable" "rc=$rc out: $out"

echo "== Result =="
printf '  %d passed, %d failed\n\n' "$PASS" "$FAIL"
[[ "$PASS" -gt 0 ]] || { printf '  a suite that ran no assertions is not a green suite\n'; exit 1; }
[[ "$FAIL" -eq 0 ]] || exit 1
