# Saved-output scoring contracts (v1)

`replay.py` freezes an explicitly selected historical result slice and replays the
existing `score.py` without model calls. It is a stdlib-only, opt-in companion;
`score.py` and its legacy loading, matching and reporting behavior are unchanged.
There is no host installation, recurring job, plugin or network client.

## Scope and limits

**Reproducible scoring is not reproducible generation, valid ground truth, or a
model-promotion decision.** Each result explicitly records generation
reproducibility as `unknown`: the exact rendered historical prompt, provider
configuration and historical execution environment have not been established.
Archived prompt templates, harnesses and schemas are bound as provenance, not
proof that a particular historical invocation used them. The harness is data;
this tool never imports or executes it.

The complete saved cases/labels, result bytes, judgments, selected config, scorer
and replay implementation are enough to reproduce the *saved-output score*.
A failed review (`review: null`) is replayable and remains in the primary run and
defect denominators. Successful replay does not make that review successful.
The scorer still aggregates by model, pooling efforts/repetitions supplied in the
slice; choose separate configs if that is not the intended analysis.

Hashes prove content consistency, **not authenticity**. Preserve the manifest
hash and source revision in an independently trusted evidence record. Someone
who can replace both artifacts and all hashes can create a different consistent
bundle. Source revisions, dates, exposure and related-case provenance are caller
declarations; the verifier does not consult Git, an issue tracker or a clock to
certify them. Shape-valid labels are not necessarily exhaustive or correct.

Private historical cases and raw responses must stay outside this public repo.
Use authorized artifact access and private run scratch; upload portable bundles
only to the appropriate private task/artifact store. Do not restore private
fixtures, Git objects or historical branches to make public tests pass.

## Freeze the config first

Declare the cutoff, slice membership (including failure/retry policy), source
revision and any authoritative case metadata **before selecting a holdout**.
Example `config.json`, next to the private input files:

```json
{
  "schema": "review-bakeoff-config/v1",
  "source": {
    "revision": "exact-source-revision",
    "provenance": "authorized immutable artifact reference"
  },
  "cutoff": "2026-01-02T00:00:00Z",
  "artifacts": {
    "cases": "dataset/cases.json",
    "prompt": "review_prompt.md",
    "harness": "run_review.py",
    "output_schema": "output_schema.json"
  },
  "result_dirs": ["results/primary"],
  "diagnostic_result_dirs": ["results/diagnostic"],
  "case_metadata": {}
}
```

All paths are relative to the config directory. Absolute paths, `..`, noncanonical
paths and symlinks are refused. Select directories explicitly; each contributes
its immediate nonhidden, non-underscore `*.json` files, matching the legacy loader. There
must be at least one primary result. Diagnostic retries are frozen and counted
separately, **never substituted for failed primary attempts**. Empty diagnostic
lists are valid. Missing directories, unknown cases, malformed/partial scorer
inputs and duplicate case IDs or result identities refuse the whole bundle;
there is no silent exclusion or denominator shrinkage. Result identity is
`(case_id, model, effort, rep)` within a role; missing effort/rep are explicit
unknowns, not a license to admit duplicates. Filename stems must be unique within
a role so legacy judgment keys remain unambiguous.

Add an optional `artifacts.judgments` path for a JSON object mapping
`<primary-result-stem>#<zero-based-finding-index>` to `valid-other`, `invalid` or
a defect ID in that result's case. Omission freezes an explicit empty object.
Unknown finding references and invalid verdicts refuse. Automatic location
matches still take precedence, exactly as in the original scorer.

## Task/scorer contract

The versioned contract fixes inclusive line slack at 15, the existing path suffix
match, model aggregation, retained failed-run denominators, and refusal of
unknown cases/incomplete label shapes. Each case has a unique nonempty `id` and
an explicit `defects` list. Each defect has a unique nonempty `id`, `file`, positive
integer `line_start <= line_end`, and `minor`, `major` or `critical` severity.
An empty defects list represents the caller's explicit clean-case labels; it is
not an inferred absence of defects.

Each result requires nonempty `case_id` and `model`, finite nonnegative
`latency_s`, an explicit `review` object or null, and optional nonnegative integer
usage counts in 0–2^63−1. A non-null review requires integer `score` in 0–5 and `findings`;
findings require file, positive integer line and a recognized severity. Optional
effort is a nonempty string; optional rep is a positive integer. This validates
the fields consumed by the scorer, not every requirement of the archived output
schema. It does not silently tighten the legacy parser or pretend older outputs
satisfied a newer generation schema. Original bytes remain unchanged.

## Time split and contamination

For each case, optional `case_metadata` can declare:

```json
{
  "synthetic-case": {
    "observed_at": "2026-01-02T00:00:00Z",
    "date_source": "authoritative source event reference",
    "exposure": "unexposed",
    "related_group": "audited-related-case-group",
    "group_source": "relationship audit reference"
  }
}
```

- Both cutoff and observed dates require timezones. Before the cutoff is `train`;
  equality and later are `holdout`. Offsets are compared as instants.
- Union related-group declarations, exact normalized cases (ignoring case/defect
  IDs and defect ordering), identical repository/head snapshots, and matching
  nonempty defect-location sets scoped by repository. The latter is deliberately
  conservative: changing descriptive text or severity cannot disguise related
  labels. Repeated locations can quarantine distinct cases; this is not semantic
  duplicate detection. Supply audited group provenance; unknown relationships
  cannot be retrospectively certified by this heuristic.
- A group with prior exposure or dates on both sides of the cutoff is
  `contaminated`, partition `quarantine`, for **all** its cases/observations.
- Missing dates/date sources, unknown exposure, or missing group provenance make
  the entire group `unknown`, never clean holdout. An exposed group remains
  contaminated even if dates are missing.
- Otherwise status `clean` means **clean by the supplied declarations**, not an
  independent contamination audit. Published/examined historical bakeoff cases
  must be declared `exposed`; do not retroactively call them untouched holdouts.

Scoring still covers every primary result. Partition/quarantine does not
silently remove observations from score denominators; holdout status is reported
separately, not used to manufacture a model comparison.

## Build and verify

```sh
python3 -B eval/review-bakeoff/replay.py bundle \
  --config /private/slice/config.json --output /private/portable-bundle
python3 -B /private/portable-bundle/tool/replay.py verify \
  --bundle /private/portable-bundle
```

The output parent must already exist. The bundle contains:

- exact `inputs/` bytes, plus the explicit empty judgments file when omitted;
- unchanged `results/*.json` and separate `diagnostic/*.json`;
- `tool/score.py` and `tool/replay.py` (run the bundled verifier to pin both);
- per-result `contracts/*.manifest.json` sidecars with artifact hashes, scorer
  contract, per-result metrics, missing generation evidence and holdout status;
- `scores.md`, exactly the existing scorer's rendered primary-result table;
- canonical `report.json` with eligible/replayed/failed/diagnostic row counts and
  holdout statuses; and `manifest.json` binding every file and source path.

Sidecars are **outside** result directories, so legacy `*.json` enumeration never
mistakes them for model outputs. Canonical JSON uses sorted keys, UTF-8, compact
separators, a trailing newline and no nonfinite numbers. There is no generated
wall-clock timestamp in canonical output. Identical frozen inputs and tool bytes
produce identical bundle bytes. Python 3.10+ is required; record the actual Python
version and elapsed time in execution evidence, outside canonical output.

Verification checks inventory and every hash, pins the executing tool/scorer,
then recomputes the sidecars, partitions, metrics and report; changing a
self-described claim without matching recomputation refuses. Publication stages
all files before renaming the complete directory into place. Same-input repeats
and cooperating same-input writers are idempotent. Different existing evidence
is never intentionally overwritten; choose a new output path. CLI success emits
a JSON report and exit 0. Contract failures emit `{"error":"stable_code",
"detail":"..."}` on stderr and exit 2; do not treat a refused bundle as a partial
successful metric.

## Offline checks and evidence

```sh
python3 -B -m unittest discover -s eval/review-bakeoff -p 'test_*.py' -v
bash test_case_61df0b37_review_bakeoff.sh
```

The existing shell regression suite discovers all `test_*.py`, including the new
contract suite. `.github/workflows/eval-replay.yml` also runs the contract tests
explicitly on relevant PRs and main pushes, making their producer visible to the
orphan-suite gate without editing the shared CI workflow. It uses synthetic
inputs, read-only repository permissions and a five-minute timeout. Tests
use synthetic temporary inputs, not the private historical dataset. They include
portable CLI replay, deterministic bytes, failure denominators, hash mutation,
missing/extra files, invalid contracts, judgments, symlink refusal, concurrent
writers and time-split contamination. They never call inference.

For a live acceptance record, freeze the **largest authorized historical set**
and state its source revision, artifact/manifest hashes, exact commands, Python
version, elapsed time, row counts, failed and diagnostic counts, and partition
integrity. Metric denominator is all selected eligible historical records,
including failures; numerator is records with validated contracts and successful
offline scoring replay. Record before/after `n/N`, separately from unknown
generation provenance and holdout contamination. Do not substitute report-table
counts for raw rows, claim an unavailable private slice has zero coverage, or
use synthetic test coverage as a live measurement. No new inference or
quality/promotion claim is authorized by this contract.
