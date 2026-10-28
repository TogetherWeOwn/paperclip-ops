# Risk-based evidence obligations

Consumer: **author/reviewer**. Generate an advisory proof-obligation report from a
caller-supplied change snapshot, before requesting the existing independent
review. This connects the [canonical risk classifier](../../.github/scripts/review_trigger.py)
to the [existing lexical mapping helpers](deterministic.py); it is not another
review router, scanner or merge gate.

## Offline command

Requires Python 3.10+, no packages, network, credentials or dependency preparation:

```sh
python3 -B ops/review_tier1/proof_obligations.py < input.json
python3 -B -m unittest discover -s ops/review_tier1 -p 'test_proof_obligations_acceptance.py' -v
```

The CLI reads stdin and fixed local helper modules, emits one JSON object to
stdout and writes no files. No supplied paths, diffs, labels or criteria are
executed or used to read files. An absolute script path also works from another
working directory. The [quality workflow](../../.github/workflows/quality-gates.yml)
runs the independently authored acceptance suite before installing gate tools;
it does not run an obligation report against live PRs automatically.

Input is exactly this object shape (the SHAs and diff below are fictitious):

```json
{
  "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "changed_files": ["pricing.py"],
  "labels": [],
  "additions": 4,
  "deletions": 0,
  "diff": "diff --git a/pricing.py b/pricing.py\n--- a/pricing.py\n+++ b/pricing.py\n@@ -0,0 +1,4 @@\n+def pricing_total(items):\n+    return sum(items)\n+def test_pricing_total_sums_items():\n+    pass\n",
  "criteria": ["pricing_total sums all items"],
  "boundary_criteria": []
}
```

All fields are required; unknown fields and duplicate JSON keys are refused.
Commit identities must be full lowercase 40-character hexadecimal strings.
Counts must be nonnegative integers, not booleans. All four list fields contain
nonempty strings; duplicates within either criteria list are refused. Empty
lists and an empty diff are valid inputs, but supply no missing evidence.
Malformed JSON, wrong types and invalid values yield a refusal report, not an
empty clean result. Diagnostics use stable codes and do not echo raw input.

Supply complete changed paths (both names for renames), labels and counts from
the intended revision. Risk classification uses those supplied values, not
paths inferred from the diff. The diff supplies best-effort symbol/test names.
The adapter does **not** authenticate Git identities, validate that these inputs
agree, establish criteria quality, or verify that a criterion describes a real
failure case. A head SHA in a report is an input identifier, not provenance.

## Outcomes and blind spots

| Exit | `outcome` | Meaning |
| --- | --- | --- |
| 0 | `mapped` | All required and supplied criteria have lexical symbol/test-name mappings |
| 1 | `missing` | Required criteria or their mappings are missing |
| 2 | `unmeasurable` | Input was refused; identities and risk are null |

Every report uses `schema: review-proof-obligations.v1`, `mode: warn`,
`merge_authority: false` and `execution_proven: false`, including exit 0.
These outcomes never authorize merge, promotion, deployment or skipping review.

- `risk.level` is `ordinary` or `elevated`; `risk.reasons` comes unchanged from
  `is_risky`. The existing path, label and strictly-more-than-600-line rules
  apply. Audit sampling is not a risk signal. This is not attestation T0/T1/T2.
- Ordinary changes require nonempty, fully mapped `criteria`. Elevated changes
  additionally require nonempty, fully mapped `boundary_criteria`. Optional
  boundary criteria on ordinary changes are still checked. The same text may
  appear once in each list; the adapter cannot judge whether that is sufficient.
- `obligations` preserves input order, acceptance first, with `kind`, `criterion`,
  `symbol`, `test` and `mapped`. Stable `missing` IDs are `acceptance-criteria`,
  `acceptance-mapping`, `boundary-criteria`, `boundary-mapping`, in that order.
  Missing criteria and unmapped criteria are different findings.
- `layers` names `tier0`, `tier1` and `tier2`, each with
  `execution_status: not_observed` and declared `blind_spots`. Config-dependent
  scanner skips are not clean scans; lexical mappings are not executed tests;
  independent judgment and runtime evidence remain necessary.

The example's test body is intentionally `pass`: a lexical match can exist
without a meaningful assertion or any execution. The mapping heuristic may also
match context, unrelated names or snippets. Independently inspect criteria,
assertions and exact-head execution results; this report proves none of them.
Layer declarations describe limits, not observations of a gate run.

## Repeated conventions become governed rules

Keep using the existing feedback loop rather than creating another registry:

1. Classify observed review findings with
   [review-finding-buckets.json](../../review-finding-buckets.json). Its buckets
   name the deterministic, cheap-evidence and independent-judgment layers that
   should have caught a finding; they are not evidence that any layer ran.
2. Use [review_routing_weekly.py](../../review_routing_weekly.py) for existing
   bucket counts and promotion candidates. The taxonomy's three-findings-in-90-days
   rule identifies repeated non-Tier-2 conventions for a lint/test/rule or prompt
   change; it does not author, install or approve one automatically.
3. Use [warn_first_inventory.py](../../warn_first_inventory.py) for existing
   backlog/scope evidence. A local mirror is not proof of the central rule
   engine's accuracy. Incomplete scans and out-of-scope findings are not zero
   backlog or false-alarm calibration.
4. Make the concrete convention fix, fixtures and documentation in its existing
   feature/fix PR under normal review and promotion policy. Preserve the pinned
   [central review-rules caller](../../.github/workflows/review-rules.yml), its
   WARN/shadow mode and all existing required CI/review gates. This adapter
   neither promotes rules nor changes routing or enforcement.

## Verification and measurement

The focused acceptance command above covers classifier/helper parity, missing
obligations, malformed inputs, deterministic output, arbitrary working directory
and no file writes. Existing helper checks:

```sh
python3 -B -m unittest discover -s ops/review_tier1 -p 'test_review_tier1.py' -v
python3 -B -m unittest discover -s .github/scripts -p 'test_review_trigger.py' -v
```

Keep live sample IDs, immutable inputs, commands and before/after counts on the
private native task, not in this public source. Measure structured-report
availability separately from confirmed detections, misses, false positives,
review rounds and post-merge CI execution. An empty or unlabelled outcome sample
is no data, not proof of improved accuracy. Source publication does not activate
a service or install runtime automation. Rollback is a reviewed revert of this
adapter, its CI test step and documentation; no host action is involved.
