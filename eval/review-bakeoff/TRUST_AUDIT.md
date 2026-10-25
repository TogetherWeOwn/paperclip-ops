# Saved reviewer trust audit

`trust_audit.py` is a read-only, standard-library checker for saved bake-off
inputs. It makes no network or model calls and does not rewrite `score.py`,
source ledgers, provider settings or historical scores.

```sh
python3 -B eval/review-bakeoff/trust_audit.py \
  --cases /authorized/data/cases.json \
  --results /authorized/data/results \
  --judgments /authorized/data/judgments.json

PYTHONPATH=eval/review-bakeoff python3 -B -m unittest -v \
  test_batch test_build_cases test_run_review test_score test_szz_pairs test_trust_audit
```

Omit `--judgments` when no per-finding judgments exist. Case-admission decisions
and dataset labels are **not** per-finding judgments. Keep real review text and
raw ledgers in their existing authorized location, not in this public repository.
The CLI returns stable JSON and exit 0 for a valid audit, including abstention;
input errors return a JSON `error` on stdout and exit 2. Exit 0 is not a quality
approval. An empty existing results directory is valid; a missing directory is not.

## Input contract

The Python entry point is
`audit(cases_document, results, judgments) -> dict`. Pass the raw cases document
`{"cases": [...]}`, a list of `(result_key, result_object)` pairs and a judgment
object. Inputs are never mutated. The CLI reads immediate `*.json` files using
filename stems as keys, excluding `_*.json`, other extensions and nested files.

Cases have unique nonempty `id` values and a `defects` list. Defect IDs are unique
within a case (not globally); `invalid` and `valid-other` are reserved. Locations
require nonempty file paths and positive integer lines, inclusive ordered ranges
for defects, and severity `minor`, `major` or `critical`. Results have unique keys,
a known `case_id` and explicit `review`: either null (a counted failed run), or an
object with integer `score` 1–5 and a findings list with valid locations. Additional
metadata is allowed but is not treated as evidence provenance.

Judgments are `{"result-key#finding-index": "invalid" | "valid-other" | defect_id}`.
Indices are zero-based, canonical decimal indices in the saved findings list.
Unknown cases, duplicate identities, orphan judgments, invalid labels and malformed
records reject the whole audit with a reason and relevant identity. The JSON
loader rejects duplicate object keys at every depth and non-finite constants;
none can silently disappear through a dictionary conversion. Repair the source
or choose an explicitly documented valid subset; do not quietly skip bad rows.

## Report and denominators

Each `dimensions` entry has `status: measured|abstain`, a nonnegative integer
`denominator`, `value` (null for abstention) and an explanatory `reason`.

- **dataset_audit:** inventory counts `cases`, `results`, `failed_results`,
  `findings`; denominator is **case rows + result rows**, not a statistical sample.
  Abstains when both inventories are empty.
- **scoring_mechanism_attribution:** denominator is **findings**. Calls the existing
  `score.match_defect` directly, preserving file suffix matching, inclusive
  15-line slack and first-match order. `automatic_location` counts hits regardless
  of judgment. `explicit_defect`, `valid_other`, `invalid` count explicit labels.
  `unjudged` means no explicit label, including automatic hits. `conflicts` counts
  automatic hits labeled invalid, valid-other, or a different known defect.
  These are **overlapping axes**, not a partition or independent correctness.
  With no findings this dimension abstains, rather than reporting a measured zero.
- **calibration, ordering_bias, verbosity_bias, causal_mechanism_attribution,
  uncertainty:** this minimal input format cannot establish the required evidence;
  these dimensions always abstain, with denominator **0 supported independent
  observations/comparisons**. They do not estimate an interval or claim debiasing.

`coverage` gives `explicit_judgments`, `findings`, and
`nonconflicting_explicit_credit`: positive explicit labels (defect or valid-other)
that do not conflict with a location hit. This count is an **audit eligibility
filter**, not verified truth, precision, recall or a provider ranking. In
particular, zero with no labels means missing evidence, not zero review quality.

`queue` contains `total`, at most 100 identifier-only `items` and `truncated`.
Unjudged findings, judgment conflicts and failed results get fixed reasons and
record identifiers, never copied review summary/title/rationale. Runs sort by key;
finding order stays unchanged because it defines judgment identity. Queue totals
remain exact after truncation. IDs themselves come from input, so keep the report
in the same authorized boundary as its source if those identifiers are private.

## What this does not prove

A five-point quality score is not a probability of correctness. A label without
independent adjudication/sampling provenance is not a calibrated outcome. Approval,
merge survival, repeated runs and unrelated cases are not controlled comparisons.
Reordering rows or comparing summary lengths cannot establish ordering/verbosity
bias. Scoring-path attribution does not identify causal mechanisms inside a model.
Supporting those claims would require a separately specified, independently
reviewed evidence contract; this checker neither invents it nor generates data.

Synthetic tests prove the contract, not live impact. Historical mixed real/planted
bake-offs must be described as historical mixed data, not current production.
Compare legacy credit and the explicit-label filter on the **same immutable raw
snapshot**, recording source revision, content hashes, row counts, missing labels
and abstention reasons in authorized evidence. Do not run a collector or new
evaluation to fill gaps. CI runs the independent contract alongside the existing
bake-off tests. Rollback is a normal revert; there is no service or job to disable.
