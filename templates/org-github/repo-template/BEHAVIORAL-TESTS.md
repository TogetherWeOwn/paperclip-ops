# Behavioral, boundary and property tests

Consumer: agents implementing and reviewing product changes. Keep the product's
existing test framework; this is a test contract, not another test framework.

## Write the behavior before the implementation

- Name each observable claim, its inputs and its expected result. Assert on the
  public behavior, not source text, private method names or mock call counts.
- For each changed boundary, exercise the minimum, maximum, just outside each
  limit, empty/missing values, malformed inputs and relevant Unicode. Add
  cross-principal, replay and failure-propagation cases when the behavior needs
  them. Use synthetic fixtures, never production data or credentials.
- Name the invariant before generating inputs. Bound input sizes and use
  `BEHAVIOR_TEST_MAX_CASES` as the framework's examples/runs per invariant;
  `BEHAVIOR_TEST_SEED` selects a reproducible random run. Record the seed and
  the minimized failing input, then preserve that input as a deterministic
  regression. A regex match is only a candidate: reproduce a concrete semantic
  counterexample before declaring a behavioral defect. A clean regex scan is
  not evidence that the invariant holds.
- Enumerate the finite acceptance cases before running them. Account for every
  expected case and invariant, including failures, skips and timeouts. A small
  finite domain can be exhaustively enumerated; a bounded random sample cannot
  be called exhaustive. Do not increase counts or weaken assertions to make a
  run green. If Test Author owns the tests, fix the implementation, not the tests.

## Reusable CI contract

Canonical workflow:
`TogetherWeOwn/paperclip-ops/.github/workflows/reusable-behavioral-tests.yml`.
Call it at a reviewed, merged **full commit SHA**, not `main` or a moving tag.
It checks out the **caller** repository, runs only on a GitHub-hosted runner,
uses `contents: read`, does not persist checkout credentials, and inherits no
secrets. Only use it for hermetic unit/property tests, not deployment or
production checks. Public PR code is untrusted: do not add a write token,
`secrets: inherit`, `pull_request_target`, production services or host runners.

Inputs (strings):

| Input | Default | Contract |
|---|---|---|
| `test-command` | required | Repo-owned shell command; nonzero exit fails the job. Must write JUnit XML. |
| `setup-command` | empty | Optional repo-owned toolchain/dependency setup; nonzero exit fails the job. Pin tools and use the lockfile. |
| `report-path` | `behavioral-tests.xml` | Fresh repository-relative regular file, outside `.git`; no traversal. An existing report fails before tests. |
| `seed` | `1729` | Decimal integer, 0..2147483647. |
| `max-cases` | `100` | Decimal integer, 1..1000, per invariant. |
| `expected-tests` | empty | If set, exact expected JUnit testcase count, 1..1000000; otherwise at least one testcase is required. |

The job has a fixed ten-minute wall-clock limit, including setup. Missing,
empty, malformed, oversized (>1 MiB), zero-test, skipped, failed or errored
reports fail. Declared JUnit testcase counts must match the actual elements;
`expected-tests` catches missing or extra enumerated cases. A passing report
cannot rescue a failed command. Setup must not manufacture the test report.

The workflow exports `BEHAVIOR_TEST_SEED`, `BEHAVIOR_TEST_MAX_CASES`,
`BEHAVIOR_TEST_EXPECTED_TESTS` and `BEHAVIOR_TEST_REPORT` to both commands.
**The suite must wire the first two into its property library.** Environment
variables alone do not configure fast-check or Hypothesis, and the workflow
cannot infer a product invariant or detect a tautological test. For example,
fast-check's `fc.assert` options should take `seed` and `numRuns` from those
variables. A Hypothesis suite should register/load a profile using `max_examples`
from the case budget and invoke pytest with `--hypothesis-seed` from the seed.
Keep every generated input size bounded too; max-cases is not a byte-size limit.
Use the repository's established toolchain setup rather than fetching an
unversioned bootstrap script. Sanitize fixture names and output so failures
cannot publish private data in logs or reports.

A successful job prints and exposes a `summary` JSON workflow output and step
summary with schema `behavioral-test-summary.v1`, seed, max_cases, actual tests,
completed, failed, errors, skipped and optional expected_tests. Configuration
and report errors print `behavioral-test-error.v1` with code, retryable=false
and next_action. A cancellation/timeout is incomplete; it may have no report.
The counts describe JUnit cases, not generated examples or proven invariants.
Retain a framework-generated counterexample in the product's regression suite.

## Caller example and existing ci-ok wiring

Replace `<reviewed-full-sha>` with the merged workflow's forty-character commit
SHA. This Vitest example assumes the repo's tests consume the property budget;
choose an existing product command, setup and reporter, not this stack by default.
No supporting script is fetched or seeded automatically by the workflow.

```yaml
behavioral-tests:
  needs: [changes, full_run]
  # Preserve the product's existing area predicate and full-run overrides.
  if: needs.full_run.outputs.force == 'true' || needs.changes.outputs.full_run == 'true' || fromJSON(needs.changes.outputs.areas).web
  permissions:
    contents: read
  uses: TogetherWeOwn/paperclip-ops/.github/workflows/reusable-behavioral-tests.yml@<reviewed-full-sha>
  with:
    setup-command: npm ci
    test-command: npx --no-install vitest run --reporter=junit --outputFile=behavioral-tests.xml
    seed: '1729'
    max-cases: '100'
```

For the staged starter CI, replace its `test-web` or `test-bot` placeholder
**without changing its job ID, needs or if condition**. Existing `ci-ok.needs`
then already covers it. If adding a new job instead, add its job ID directly to
`ci-ok.needs`; keep the existing success/skipped versus failure/cancelled
aggregation and required-check names. Do not create a second required check.
Do not turn an unrelated change skip into a passing claim about tests.

## Adoption evidence and rollback

1. On one real product change, record its head SHA, named claims/invariants,
   enumerated expected cases and the prior test command's counts and duration.
2. Run the same head through the reusable caller. Record the Actions run URL,
   configured seed/budget, actual testcase counts, duration and all incomplete
   outcomes. Separately record counterexamples found and reproduced. Compare
   the same metric before/after; do not count workflow existence as effectiveness.
3. Only report live adoption after that caller run is verified. Local workflow
   fixtures prove the envelope, not product coverage or defect reduction.

Rollback: revert the caller/workflow PR or restore its previous pinned SHA and
test command. Keep required `ci-ok` coverage and all review/security gates. Do
not disable a required check to roll back this helper.
