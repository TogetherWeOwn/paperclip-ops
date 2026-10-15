#!/usr/bin/env bash
# platform-watchdog-gate.sh — offline gate for the watchdog detectors.
# Fails loudly; no credentials, no network writes, no board writes.
set -uo pipefail

cd "$(dirname "$0")/.."

fail() { echo "GATE FAIL: $*" >&2; exit 1; }

python3 -m unittest -v test_platform_watchdog.py test_watchdog_sections.py \
  test_watchdog_assignment_evidence.py \
  || fail "unit tests failed"

# CLI exit/JSONL contract and the read-only, no-I/O guarantee of detect()
# (case_d9292be7): temp-file subprocess runs, no credentials, no network.
python3 -m unittest -v test_platform_watchdog_cli.py \
  || fail "CLI/read-only contract tests failed"

# Positive control: a snapshot that must fire every detector family.
SCRATCH="${PAPERCLIP_RUN_SCRATCH_DIR:-$(mktemp -d)}"
cat > "$SCRATCH/positive.json" <<'JSON'
{"now": "2026-10-03T14:00:00Z",
 "agents": [{"agentId": "a1", "status": "error",
             "errorSince": "2026-10-03T13:00:00Z", "subtype": "crash",
             "owningLead": "cto"}],
 "hosts": [{"host": "192.0.2.190", "diskPct": 99, "load": 9.0,
            "vcpu": 4, "sustainedMin": 10}],
 "ciJobs": [{"repo": "r", "job": "j", "runtimeMin": 45, "baselineMin": 20}],
 "queueAge": [{"label": "self-hosted", "ageMin": 160}],
 "assignmentEvidence": {
   "schema": "watchdog.assignment-evidence.v1", "complete": true,
   "sources": {"issues": {"complete": true, "ref": "fixture:issues"},
               "focus": {"complete": true, "ref": "fixture:focus"},
               "reviews": {"complete": true, "ref": "fixture:reviews"}}},
 "issues": [{"id": "iid-1", "identifier": "case_8c5fe4cc", "inFocus": true,
             "assigneeAgentId": null, "inBacklogMin": 90,
             "isReview": false, "prAuthorAgentId": null,
             "provenance": {
               "inFocus": {"source": "focus-membership", "ref": "fixture:focus"},
               "inBacklogMin": {"source": "status-history", "ref": "fixture:history"},
               "isReview": {"source": "review-inventory", "ref": "fixture:reviews"}}}],
 "runEvents": [{"issueId": "iid-1", "identifier": "case_8c5fe4cc",
                "kind": "dispatch_stalled_issue",
                "at": "2026-10-03T13:10:00Z"},
               {"issueId": "iid-1", "identifier": "case_8c5fe4cc",
                "kind": "dispatch_stalled_issue",
                "at": "2026-10-03T13:20:00Z"},
               {"issueId": "iid-1", "identifier": "case_8c5fe4cc",
                "kind": "dispatch_stalled_issue",
                "at": "2026-10-03T13:30:00Z"},
               {"issueId": "iid-1", "identifier": "case_8c5fe4cc",
                "kind": "dispatch_stalled_issue",
                "at": "2026-10-03T13:40:00Z"}],
 "parks": [{"issueId": "iid-1", "identifier": "case_8c5fe4cc",
            "missingDispositionCount": 4}],
 "redMains": [{"repo": "paperclip-ops", "signature": "s1",
               "incidentExists": false}],
 "secretHits": [{"source": "run-log:x", "patternName": "ghp_*",
                 "valueLen": 40, "at": "2026-10-03T13:00:00Z"}],
 "reviews": [{"issueId": "iid-r", "identifier": "TOG-R", "status": "in_review",
              "isReview": true, "assigneeAgentId": null,
              "assigneeUserId": null, "prAuthorAgentId": "agent-a",
              "prUrl": "https://github.com/o/r/pull/9",
              "assignedSince": "2026-10-03T12:00:00Z",
              "updatedAt": "2026-10-03T12:00:00Z"}],
 "openProposalSignatures": [],
 "quotaLanes": [{"lane": "Muse-weekly-1", "hoursToReset": 24,
                 "headroomPct": 10}], "pendingInteractions": [{"issueId": "iid-7", "identifier": "case_4ee01500",
               "interactionId": "int-1", "kind": "request_confirmation",
               "createdAt": "2026-10-03T13:00:00Z",
               "resolverAgentId": "agent-r"}],
 "supply": {"readyNow": 0, "target": 12, "belowMin": 35.0,
            "idleAgents": 2, "censusAt": "2026-10-03T14:00:00Z"},
 "autoscalerSlices": [{"sliceId": "slice-1", "identifier": "case_9ae341a0",
               "status": "backlog", "assigneeAgentId": null,
               "unassignedSince": "2026-10-03T11:00:00Z"}],
 "shadowEmit": {"lastDecisionAt": "2026-10-03T13:00:00Z"},
 "orgStandard": {"canonical": {"templateSha": "t", "agentsSha": "a",
                               "contributingSha": "c"},
                 "repos": [{"repo": "two-web-next",
                             "templateSha": "stale", "agentsSha": "a",
                             "contributingSha": "c",
                             "inheritsOrgDefault": false,
                             "rulesetAttached": false}]},
 "assignedInProgress": [{"issueId": "iid-9", "identifier": "case_f052904a",
               "status": "in_progress", "assigneeAgentId": "agent-r",
               "liveRunCount": 0, "queuedRunCount": 0,
               "unresolvedBlockerCount": 0, "monitorNextCheckAt": null,
               "activeRecoveryAction": null,
               "newestComment": {"createdAt": "2026-10-03T10:00:00Z",
                 "body": "API Error: Request rejected (429)"}}],
 "agentRuns": [{"agentId": "agent-r",
               "lastSucceededAt": "2026-10-03T13:50:00Z"}]}
JSON
positive_out="$(python3 watchdog/detectors.py \
  --snapshot "$SCRATCH/positive.json")" \
  || fail "positive snapshot errored"
for family in agent_error host_health ci_health assignment churn red_main secret_hit stale_reviews quota_expiry pending_interactions supply_famine autoscaler_slices shadow_emit_gap org_standard stranded_429; do
  grep -q "\"watchdog/$family\"" <<<"$positive_out" \
    || fail "positive control silent for $family"
done
# stranded_429 also answers with an `unknown` when its sections are missing,
# so the family grep above cannot tell fired from absent: pin the proposal.
grep -q '"reason": "stranded after a 429 with no continuation"' <<<"$positive_out" \
  || fail "positive control: stranded_429 emitted no proposal"

# Negative control: an all-quiet snapshot fires nothing above info... but the
# required-hosts rule still holds, so expect only info-level or nothing.
cat > "$SCRATCH/negative.json" <<'JSON'
{"now": "2026-10-03T14:00:00Z",
 "agents": [{"agentId": "a1", "status": "idle"}],
 "hosts": [{"host": "192.0.2.190", "diskPct": 10, "load": 1.0,
            "vcpu": 4, "sustainedMin": 1},
           {"host": "garm-1", "diskPct": 10, "load": 1.0,
            "vcpu": 4, "sustainedMin": 1}],
 "ciJobs": [{"repo": "r", "job": "j", "runtimeMin": 20, "baselineMin": 20}],
 "queueAge": [{"label": "self-hosted", "ageMin": 5}],
 "issues": [],
 "assignmentEvidence": {
   "schema": "watchdog.assignment-evidence.v1", "complete": true,
   "sources": {"issues": {"complete": true, "ref": "fixture:issues"},
               "focus": {"complete": true, "ref": "fixture:focus"},
               "reviews": {"complete": true, "ref": "fixture:reviews"}}},
 "runEvents": [], "parks": [],
 "redMains": [], "secretHits": [],
 "reviews": [{"issueId": "iid-r", "identifier": "TOG-R", "status": "in_review",
              "isReview": true, "assigneeAgentId": "agent-x",
              "assigneeUserId": null, "prAuthorAgentId": "agent-a",
              "prUrl": "https://github.com/o/r/pull/9",
              "assignedSince": "2026-10-03T13:30:00Z",
              "updatedAt": "2026-10-03T13:30:00Z"}],
 "openProposalSignatures": [], "pendingInteractions": [],
 "quotaLanes": [],
 "pendingInteractions": [], "supply": {"readyNow": 12, "target": 12, "belowMin": 0.0,
            "idleAgents": 2, "censusAt": "2026-10-03T14:00:00Z"},
 "autoscalerSlices": [], "assignedInProgress": [], "agentRuns": []}
JSON
negative_out="$(python3 watchdog/detectors.py \
  --snapshot "$SCRATCH/negative.json")" \
  || fail "negative snapshot errored"
grep -q '"severity": "high"\|"severity": "critical"' <<<"$negative_out" \
  && fail "negative control fired high/critical"
grep -q '"type": "summary"' <<<"$negative_out" \
  || fail "negative control printed no summary"

# Writer-compatibility: inspect this candidate, never an older origin/main.
# A detector may name diagnostics without granting them mutation authority.
python3 -m unittest -v test_watchdog_writer_compat.py \
  || fail "writer-compatibility regressions failed"
python3 - <<'WRITER_COMPAT' || fail "writer/detector compatibility failed"
import ast
from pathlib import Path


def constant(path, name):
    matches = [node.value for node in ast.parse(Path(path).read_text()).body
               if isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Name) and target.id == name
                       for target in node.targets)]
    if len(matches) != 1:
        raise SystemExit(f"missing/ambiguous {name} in {path}")
    value = matches[0]
    if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id == "frozenset" and len(value.args) == 1
            and not value.keywords):
        value = value.args[0]
    result = ast.literal_eval(value)
    if not isinstance(result, set) or not all(isinstance(item, str) for item in result):
        raise SystemExit(f"invalid mutation set {name} in {path}")
    return result


allowed = constant("recovery_writer.py", "ALLOWED_MUTATIONS")
diagnostics = constant("recovery_writer.py", "DIAGNOSTIC_MUTATIONS")
compatible = constant("watchdog/detectors.py", "WRITER_COMPATIBLE_MUTATIONS")
required = {"create_pause_hold", "assign_blocker_owner", "restore_blocked", "operator_decision"}
if not required <= allowed or not required <= compatible:
    raise SystemExit("writer/detector compatibility lost a required mutation")
if "clear_pin" in allowed or "clear_pin" not in diagnostics or "clear_pin" not in compatible:
    raise SystemExit("clear_pin must remain compatible but diagnostic-only")
if not compatible <= allowed | diagnostics | {"none"}:
    raise SystemExit("detector compatibility includes an unsupported mutation")
print("writer compatibility: candidate write/diagnostic boundaries verified")
WRITER_COMPAT

# Dedupe: a repeat poll on an already-proposed signature emits an
# info-level repeat, never a second proposal. A mutation that re-proposes
# (or drops the repeat row) breaks exactly one proposal per card+signature.
cat > "$SCRATCH/repeat.json" <<'JSON'
{"now": "2026-10-03T14:00:00Z",
 "agents": [], "hosts": [], "ciJobs": [], "queueAge": [], "issues": [],
 "assignmentEvidence": {
   "schema": "watchdog.assignment-evidence.v1", "complete": true,
   "sources": {"issues": {"complete": true, "ref": "fixture:issues"},
               "focus": {"complete": true, "ref": "fixture:focus"},
               "reviews": {"complete": true, "ref": "fixture:reviews"}}},
 "runEvents": [], "parks": [], "redMains": [], "secretHits": [],
 "reviews": [{"issueId": "iid-r", "identifier": "TOG-R", "status": "in_review",
              "isReview": true, "assigneeAgentId": null,
              "assigneeUserId": null, "prAuthorAgentId": "agent-a",
              "prUrl": "https://github.com/o/r/pull/9",
              "assignedSince": "2026-10-03T12:00:00Z",
              "updatedAt": "2026-10-03T12:00:00Z"}],
 "openProposalSignatures": ["TOG-R:unassigned"]}
JSON
repeat_out="$(python3 watchdog/detectors.py \
  --snapshot "$SCRATCH/repeat.json")" \
  || fail "repeat snapshot errored"
grep -q '"type": "proposal"' <<<"$repeat_out" \
  && fail "repeat poll emitted a second proposal for an open signature"
grep -q 'stale review already proposed' <<<"$repeat_out" \
  || fail "repeat poll dropped the info-level repeat row"
grep -q '"verdict"' <<<"$repeat_out" \
  && fail "detector output grew a verdict field; JSONL rows are finding/proposal/summary only"

# Secret hygiene: names and lengths only. The expected patternName "ghp_*"
# is short with a literal asterisk; anything value-shaped (ghp_ plus 8 or
# more alphanumerics, or any 32+ char token blob) fails the gate.
grep -Eq 'ghp_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9]{8,}' <<<"$positive_out" \
  && fail "detector output carries possible secret material"
grep -Eq '[0-9]+' <<<"$(grep -Eo '"valueLen": [0-9]+' <<<"$positive_out")" \
  || fail "secret finding carries no length evidence"

# Proposal-delivery observability (case_975a7e27/case_07aa689b): the offline join of
# detector output and writer receipts. A routable cycle exits 0 with
# per-detector metrics; a dropped proposal exits 1 with a routed high alert;
# the by-design-issueless famine page is refused and not paged.
python3 -m unittest -v test_watchdog_delivery.py \
  || fail "delivery unit tests failed"

python3 watchdog/detectors.py --delivery-metrics \
  --snapshot "$SCRATCH/positive.json" \
  > "$SCRATCH/detectors.jsonl" \
  || fail "detector run errored"
grep -q '"emittedAt"' "$SCRATCH/detectors.jsonl" \
  || fail "detector records carry no emission timestamp"
grep -q '"perDetector"' "$SCRATCH/detectors.jsonl" \
  || fail "detector summary carries no per-detector metrics"

# Clean cycle: the positive snapshot with supply at target, so no
# by-design-undecidable famine page (supply_famine proposes a cross-issue
# page with no issue id; the writer correctly refuses it as undecidable).
# Everything routable must join clean.
python3 - "$SCRATCH/positive.json" "$SCRATCH/clean.json" <<'PY'
import json, sys
snap = json.load(open(sys.argv[1]))
snap["supply"] = {"readyNow": 12, "target": 12, "belowMin": 0.0,
                  "idleAgents": 2,
                  "censusAt": snap["supply"]["censusAt"]}
json.dump(snap, open(sys.argv[2], "w"))
PY
python3 watchdog/detectors.py --delivery-metrics \
  --snapshot "$SCRATCH/clean.json" \
  > "$SCRATCH/clean-detectors.jsonl" \
  || fail "clean detector run errored"

python3 recovery_writer.py --proposals "$SCRATCH/clean-detectors.jsonl" \
  > "$SCRATCH/writer.jsonl" \
  || fail "writer dry-run errored (offline, no network)"
grep -q '"type": "receipt"' "$SCRATCH/writer.jsonl" \
  || fail "writer printed no receipts"

delivery_out="$(python3 -m watchdog.delivery \
  --detectors "$SCRATCH/clean-detectors.jsonl" \
  --receipts "$SCRATCH/writer.jsonl" \
  --now 2026-10-03T14:05:00Z)" \
  || fail "clean delivery join exited non-zero"
grep -q '"type": "delivery_metric"' <<<"$delivery_out" \
  || fail "clean join printed no delivery metrics"
grep -q '"alertsHigh": 0' <<<"$delivery_out" \
  || fail "clean join reports high alerts"
grep -q '"type": "delivery_summary"' <<<"$delivery_out" \
  || fail "clean join printed no delivery summary"
grep -q 'detectorSummaryMismatch' <<<"$delivery_out" \
  && fail "clean join reports detector summary mismatch"

# Issueless-refusal proof: the full positive cycle carries the famine page,
# which names no card. The writer must exit 1 and record refused:undecidable
# for watchdog/supply_famine. The join must exit 0: that refusal is expected,
# counted, and must not raise a high alert (nothing an operator can clear).
if python3 recovery_writer.py --proposals "$SCRATCH/detectors.jsonl" \
    > "$SCRATCH/writer-full.jsonl" 2>/dev/null; then
  fail "writer accepted a by-design-undecidable proposal"
fi
grep -q '"decision": "refused:undecidable"' "$SCRATCH/writer-full.jsonl" \
  || fail "writer recorded no undecidable refusal"
grep -q '"detector": "watchdog/supply_famine"' "$SCRATCH/writer-full.jsonl" \
  || fail "undecidable refusal names the wrong detector"
undecidable_out="$(python3 -m watchdog.delivery \
  --detectors "$SCRATCH/detectors.jsonl" \
  --receipts "$SCRATCH/writer-full.jsonl" \
  --now 2026-10-03T14:05:00Z)" \
  || fail "issueless famine refusal raised a delivery alert"
grep -q '"alertsHigh": 0' <<<"$undecidable_out" \
  || fail "issueless famine join reports a high alert"
grep -q '"severity": "high"' <<<"$undecidable_out" \
  && fail "issueless famine join emitted a high alert"
grep -q '"detector": "watchdog/supply_famine"' <<<"$undecidable_out" \
  || fail "issueless famine join dropped the famine metric"

# Drop proof: feed the writer every routable line except one proposal, then
# join against the clean detector output with a --now past the stall
# threshold.
python3 - "$SCRATCH/clean-detectors.jsonl" "$SCRATCH/subset.jsonl" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
rows = [json.loads(line) for line in open(src) if line.strip()]
dropped = False
with open(dst, "w", encoding="utf-8") as handle:
    for row in rows:
        if not dropped and row.get("type") == "proposal":
            dropped = True
            continue
        handle.write(json.dumps(row, sort_keys=True) + "\n")
if not dropped:
    sys.exit("positive snapshot emitted no proposal to drop")
PY
python3 recovery_writer.py --proposals "$SCRATCH/subset.jsonl" \
  > "$SCRATCH/writer-subset.jsonl" \
  || fail "writer dry-run on subset errored"
# Detector emittedAt stamps are wall-clock, so age the check past the
# stall threshold from the real clock, not from the fixture time.
stall_now="$(date -u -d '+2 hours' +%Y-%m-%dT%H:%M:%SZ)" \
  || fail "cannot compute stall reference time"
if drop_out="$(python3 -m watchdog.delivery \
    --detectors "$SCRATCH/clean-detectors.jsonl" \
    --receipts "$SCRATCH/writer-subset.jsonl" \
    --now "$stall_now")"; then
  fail "dropped proposal went unreported (delivery exited 0)"
fi
grep -q '"severity": "high"' <<<"$drop_out" \
  || fail "drop produced no high alert"
grep -q '"route": "ceo-hourly-routine"' <<<"$drop_out" \
  || fail "stall alert routes nowhere"
grep -Eq 'ghp_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9]{8,}' <<<"$drop_out" \
  && fail "delivery output carries possible secret material"

# Mutation control (case_d9292be7): the CLI/read-only tests must fail when main()
# or detect() breaks the contract. Runs on temporary copies; the checkout is
# never edited.
python3 verification/platform-watchdog-cli-mutation-gate.py \
  || fail "CLI/read-only contract mutation gate failed"

echo "GATE PASS: watchdog detectors read-only, all families fire, writer-compatible, delivery join clean with drop/stall and issueless-refusal proofs"
