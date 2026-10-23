# Paired review-gate acceptance contract

Test-only handoff. No runtime implementation, installation, dispatch, provider call,
capacity change, or gate waiver is included. The source port must build on the exact
published test commit and preserve this directory byte-for-byte. Test corrections
belong to the independent test author, not the implementation author.

## Source and import contract

Python 3.12+; standard library only. The sanitized source bundle is an external,
immutable prerequisite, not fetched by this harness. Its ZIP SHA256 is
`9340d954b3b38af59dbeb152a43fc35f3d15e473cdd3e7ac8819e4f7bb0a1235`;
the mapping manifest SHA256 is
`69855f70e5839606191381e10a190eefa821a6ad3fc4b41f786d77c76a374d40`.
`mutations.py:PINS` records the eight executable source/test member hashes used for
baseline replay. The post-fairness gate hash is
`70f037b0bac7199b36477a5bfc6c7e90b06482b8a8c4de79571bd998bdeef4a5`;
the cursor helper hash is
`a90a5561cea29bf45a1acd48de10aa4e73ca1ac229257426e5128ad98f34850a`.

Pass an explicit source directory containing these sibling modules:

- `review_gate.py`: `main()`, `fair_order`, `due_rank`, `gate_admit`, `ci_green`,
  `required_checks`, and the retained baseline boundaries.
- `pr_controller.py`: the actual paginated `fetch_repository(full, env, query=None)`
  and its `gh(args, env)` transport. No bulk-list stand-in.
- `review_dedup.py`, `review_findings.py`, `firstpass_review.py`,
  `review_gate_runtime.py`: sanitized sibling dependencies.
- `test_review_gate.py` and `test_review_gate_fairness.py`: retain all fifteen
  supplied baseline assertions; `--legacy` requires them.

A port can place these siblings in an isolated directory such as `ops/review_gate/`.
Do not silently import the existing, distinct productivity helpers. The runner
verifies imported module paths against the requested directory.

## Commands

Run from the repository root, with an existing owned scratch directory selected by
`PAPERCLIP_RUN_SCRATCH_DIR` or `PAPERCLIP_SCRATCH_DIR`:

```sh
# Immutable sanitized baseline: expected exit 1, 45 passes / 47 tests,
# exactly two loudness failures, no errors or skips.
python3 -B verification/review_gate_acceptance/run.py \
  --source-dir "$SOURCE_DIR" --legacy --json "$PAPERCLIP_RUN_SCRATCH_DIR/baseline.json"

# Baseline only: expected exit 0; creates a NEW evidence directory.
# 17 controls detected; restored existing contracts 45/45 pass.
python3 -B verification/review_gate_acceptance/mutations.py \
  --source-dir "$SOURCE_DIR" --output-dir "$PAPERCLIP_RUN_SCRATCH_DIR/mutation-evidence"

# Builder acceptance: expected exit 0, all 47 tests, zero skips/errors/failures.
python3 -B verification/review_gate_acceptance/run.py \
  --source-dir ops/review_gate --legacy --json "$PAPERCLIP_RUN_SCRATCH_DIR/candidate.json"
```

`--select Class` or `--select Class.method` is for focused diagnosis, not final
acceptance. Final acceptance must include the full suite and `--legacy`. The Builder
must wire that full command into the port's GitHub-hosted CI. This test-only branch
has no source implementation and is not standalone merge/release approval.

## Required behavior and evidence

| Contract | Evidence |
|---|---|
| Cursor inventory | 46 unique PRs across three pages with a duplicate; page-three-only eligible head; actual helper and exact query/cursor arguments observed. |
| Fail closed | Missing/empty/repeated cursor, 100-page bound, GraphQL error with data, malformed later page; partial heads never reach dedup/dispatch/state. |
| Failure signal | Name the failed repository AND return a nonzero integer from `main()` (or raise nonzero `SystemExit`); real CLI entry block must also exit nonzero. Healthy repositories still proceed. No approved alert substitute exists in this contract. |
| Fairness | Two and three same-rank lanes, uneven populations, pinned and cross-rank priority, batch boundaries 4/6/8/15, first-pass filtering before the existing batch cut. |
| Guard regressions | Exact head, draft/check inventory/required CI, latest check attempts and advisory semantics; completed/live/queued dedup, backoff, STOP, focus threshold, reviewer fences, receipt validation, existing first-pass host/provider/fallback behavior. |
| Safety | Network/process calls denied before source import; neutral environment; scratch-only state; DB/credential/dispatch/findings/host reads mocked. Harness denial and unique-state controls included. |

`mutations.py` writes each command's case-level JSON/log plus `matrix.json`, asserts
that controls fail by the intended assertion (not an unrelated error), verifies
input hashes unchanged, removes its temporary mutant copies, and reruns every
previously green case plus all fifteen legacy tests. Sixteen controls are
**green-before / red-mutant**. The silent-success control is **already red on the
baseline**: it confirms rejection, not a demonstrated loudness fix. No green
loudness implementation is claimed.

The baseline's per-repository log-and-continue behavior returns success; both
`FailureSignalTests` intentionally fail. Keep those tests red until the Builder
implements the narrow non-success result AND CLI propagation without removing
healthy-repository isolation or any existing admission guard.

## Limits

- The CLI test executes the source's actual `__main__` AST block with mocked module
  globals; it does not spawn the live program or exercise an OS service manager.
- SQL routing, transport, dispatch receipt, host readings and findings integrations
  are fixtures. Assertions prove source predicates, not live DB/provider behavior.
- The baseline has no direct final-review host-pressure guard. First-pass pressure
  failures fall back to normal review. This contract preserves that behavior,
  rather than inventing a new host guard. Any policy gap needs separate disposition.
- Optional docs-lane implementation/config/tests were absent from the sanitized
  source. Its integration is not claimed accepted here.
- No required CI, independent review, merge, installation, staging or production
  result is established by offline acceptance. Existing security/review/release
  gates remain required on the paired implementation.
