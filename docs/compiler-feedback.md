# Bounded compiler-feedback reports

`scripts/quality-gates/compiler_feedback.py` accepts or rejects an **observed**
compiler-feedback trace. It is an offline, read-only evaluator, not an autonomous
repair loop, compiler runner or execution guard. It cannot stop, undo or roll back
an external attempt. Diagnostics are inert strings; the helper does not execute
commands, edit files, install dependencies, fetch missing objects, access
credentials or call GitHub.

The existing [Tier-0 template](../templates/org-github/tier-0/tier-0-quality-gate.yml)
remains a one-shot lint/typecheck surface with its existing job timeout and
report-only default. This helper does not change that template, weaken other
quality/security checks or install another enforced product gate. A consumer
must separately supply the trace, pin the evaluator revision and retain its
normal CI/review/deployment gates.

## Invocation and policy

From a repository checkout containing the evaluator:

```sh
python3 scripts/quality-gates/compiler_feedback.py \
  --repo /path/to/local/repository \
  --base "$BASE_SHA" --head "$HEAD_SHA" --trace /path/to/trace.json
```

`BASE_SHA` and `HEAD_SHA` must be full lowercase immutable commit IDs (40 hex
characters for SHA-1, 64 for SHA-256), available locally. Branches, tags and
abbreviations are refused. The supplied repository is inspected through fixed
Git argument vectors, with replacement refs, external diff/textconv drivers,
hooks, inherited Git overrides and lazy fetching disabled. Dirty worktree/index
content does not substitute for immutable evidence.

The consuming repository must have `.compiler-feedback.json` at **BASE**, not
just in its candidate or worktree. Example v1 policy:

```json
{
  "version": 1,
  "budgets": {
    "attempts": 2,
    "elapsed_seconds": 10,
    "changed_paths": 4,
    "changed_lines": 12
  },
  "allowed_paths": ["src/*"],
  "protected_paths": ["acceptance/owner_contract.py"]
}
```

Budget counts are nonnegative integers; elapsed seconds may be a finite,
nonnegative fractional number. Booleans are not numbers. Path patterns use
`fnmatchcase`, like the [documentation gate](docs-drift.md), not shell expansion
or normalized filesystem paths. In this matching dialect `*` can match `/`.
Allowed patterns must be nonempty as a list. Protected patterns may be empty;
the built-in protections still apply. Absolute paths and empty, `.` or `..`
components are refused.

The base policy is immutable within a trace. v1 requires `policy_sha` and
`tool_sha` to equal BASE everywhere. This is attribution, not self-attestation:
the **caller must pin the deployed evaluator to the claimed tool revision**.
This repository does not invent a default policy for consumers or claim adoption
by any product.

## Trace v1

The root object has exactly these fields:

| Field | Meaning |
| --- | --- |
| `schema` | `compiler-feedback-trace.v1` |
| `source` | `actions` or `fixture` |
| `base_sha`, `head_sha` | Exact CLI commit IDs |
| `policy_sha`, `tool_sha` | BASE in v1 |
| `before` | Complete compiler record at BASE |
| `attempts` | Nonempty list of observed attempts, in order |

An attempt has exactly `index` (integer, contiguous from 1), `parent_sha`,
`candidate_sha`, `elapsed_seconds`, and `compiler`. Duration covers the **whole
external attempt**, including its compiler run; evaluator wall-clock time is not
a substitute. Missing, nonfinite, negative, boolean or overflowing durations are
unknown, not zero. Fractional durations and the `elapsed_seconds` budget are
compared as exact JSON decimals, never binary floats, so a boundary such as
0.1 + 0.2 at a 0.3 ceiling classifies exactly; reported durations remain JSON
numbers. The initial failed-before compiler is not a repair attempt
and does not consume an attempt duration.

Every compiler record has exactly:

- `sha`: BASE for `before`, the corresponding candidate for an attempt;
- `policy_sha` and `tool_sha`: BASE;
- `status`: `failed` or `passed`;
- `complete`: the boolean `true`;
- `exit_code`: a positive integer for failure, zero for pass;
- `diagnostics`: a list of strings, empty on pass. `before` must contain at
  least one non-whitespace diagnostic;
- `actions`: the receipt below for source `actions`, or `null` for `fixture`.

Actions receipts contain exactly `run_id` (a positive decimal string),
`run_attempt` (a positive integer), `head_sha` (the compiler's exact SHA) and
`conclusion` (`failure` or `success`, matching the compiler). A repeated
run/attempt pair is invalid; run IDs are decimal identities, so a zero-padded
spelling of the same run and attempt is still a replay. Supplied receipts are
**not authenticated** by this offline helper.

Each candidate must have exactly one direct Git parent, equal to its recorded
parent. Parentage follows Git's own commit-header grammar — the `tree` header
first, then the consecutive `parent` headers after it; a `parent` line placed
after other headers proves nothing. The first parent is BASE, each subsequent
parent is the preceding candidate, and the last candidate is HEAD. Merge
candidates, omitted commits, post-success attempts and unrelated histories are
refused. A different final tree ID is not proof of a repair: HEAD's recursive
file listing must actually differ from BASE's, so empty commits and
empty-subtree-only candidates are `no_repair_change`. Intermediate compilers
must fail; a complete pass terminates the trace. Duplicate JSON keys, unknown
fields, invalid UTF-8/JSON, wrong types and missing evidence fail closed.

## Scope and inclusive ceilings

Consumption is derived from **every** direct parent-to-candidate Git diff,
using NUL-delimited, no-renames output. It counts:

1. Observed attempt records.
2. The sum of their full durations.
3. The union of touched paths across all steps.
4. Added **plus** deleted line churn across all steps, not the final net diff.

A rename counts both paths and both sides' lines. Deletions count. Reverted
intermediate edits still count. Binary counts, symlink/gitlink/type/mode changes
and malformed or inconsistent Git diff records are unmeasurable, never free
scope. Whitespace, tabs, newlines, Unicode and option prefixes in valid UTF-8
Git paths are treated literally, not as arguments or commands. Line counts use
fixed-name copies of immutable blob bytes in disposable scratch storage outside
the inspected repository, removed on completion. Git's no-index textual diff
uses a fixed Myers algorithm there; worktree and `info/attributes` cannot force
binary acceptance or affect churn. Any NUL byte in either blob is refused as
binary before counting. Scratch allocation/write failures are structured tool
failures; no inspected repository, index, ref or input file is written.

Protected scope wins over allowlists. Built-in protected paths are:

- `.compiler-feedback.json`;
- `.github/*` and `scripts/quality-gates/*`;
- files below a `test` or `tests` directory;
- filenames beginning `test_` or containing `_test.`, `.test.` or `.spec.`;
- all matches of the base policy's `protected_paths`, including author-owned
  acceptance files with unconventional names.

Any edit, deletion or rename touching these paths is rejected, even if it
strengthens a test, removes the policy or is reverted later. The existing
[floor guard](../scripts/quality-gates/floor_guard.py) is unchanged; this stricter
scope rejection applies only to accepting a supplied compiler-feedback trace.

All evidence and step diffs are validated before budget classification. A final
pass **at** every ceiling is accepted. An overage rejects even a final pass.
A final failure exactly at a ceiling is exhausted; failure below every ceiling
is incomplete, not repaired. Budget dimension precedence is attempts, elapsed
seconds, changed paths, then changed lines.

## Report and exit contract

Exactly one JSON object is written to stdout for an evaluation:

| Exit | Status | Code and dimension |
| --- | --- | --- |
| 0 | `repaired` | `repaired`, dimension `null` |
| 1 | `exhausted` | `budget_exceeded`, one of the four budget dimensions |
| 1 | `exhausted` | `protected_path` / `protected_paths`, or `outside_scope` / `allowed_paths` |
| 2 | `unmeasurable` | Specific refusal code, dimension `null` |

All reports use `schema: compiler-feedback-report.v1`, with `status`, `code`,
`dimension`, `consumed`, `paths`, `metrics`, `evidence`, boolean `retryable` and
`next_action`. Failure has a nonempty remediation action. Measured reports echo
exact base/head/policy/tool IDs. Unknown consumption is `null`, never invented
zero. `paths` is sorted and JSON-escaped without Git quoting.

Unmeasurable codes are `invalid_trace`, `invalid_duration`, `invalid_policy`,
`invalid_sha`, `git_failed`, `git_unavailable`, `invalid_diff`,
`invalid_provenance`, `incomplete_compiler`, `no_failed_before`,
`no_repair_change`, `incomplete_trace`, `fixture_only`, `trace_unavailable` and
`resource_limit`. Git failure, missing Git/trace and incomplete observations are
marked retryable; that hint does not grant permission to rerun a compiler or
external workflow. CLI usage errors such as missing required options use
argparse's normal stderr and exit 2; they are not trace evaluation reports.

## Evaluator resource limits

Evidence budgets bound a trace, not the evaluator. A single-line blob can be
arbitrarily large at negligible path/line cost, so the evaluator enforces its
own fixed `RESOURCE_LIMITS` on every evaluation:

| Limit | Ceiling |
| --- | ---: |
| Trace file bytes | 1,048,576 |
| Bytes per immutable blob (policy included) | 1,048,576 |
| Cumulative blob bytes processed | 8,388,608 |
| Attempt records | 64 |
| Git invocations | 1,024 |
| Stdout bytes per Git operation | 4,194,304 |
| Whole-evaluation monotonic seconds | 30 |

The trace is read, byte-counted and deadline-checked before any Git process
launches; an over-long attempt list is likewise refused before Git traversal.
Every read is bounded by the remaining allowance plus one sentinel byte, so an
oversized stream is refused before it is fully buffered; the child process is
stopped, its pipe closed and scratch storage removed. No CLI option, trace
field, base-policy field or environment value can raise these ceilings — an
unknown `resource_limits` field is refused by the strict v1 schemas like any
other unknown field. Crossing a ceiling returns exit 2,
`unmeasurable/resource_limit`, dimension `null`, `retryable: false`, with
unknown consumption left `null` and no repair metrics. Like every refusal here,
it rejects the supplied evidence; it cannot undo an attempt that already ran.

## Metrics, live evidence and verification

`metrics` separates `fixture`, `actions_reported` and `actions_verified`. Each
bucket has `attempts`, `elapsed_seconds` and `repairs`.

- Validated synthetic observations contribute only fixture attempts/time, never
  repairs; a passing fixture-only trace is `unmeasurable/fixture_only`.
- Supplied Actions observations contribute only reported attempts/time; an
  accepted report adds one reported repair. This is **not** proof of a real
  compiler invocation or repair.
- `actions_verified` always stays zero. `evidence.receipts_verified` is always
  `false`. Only an independently authenticated consumer can publish verified
  real-work attempt/duration/outcome counts with Actions receipts.

The independently authored [acceptance module](../test_compiler_feedback_contract.py)
constructs synthetic Git histories, compiler records and Actions-shaped
receipts; the supplemental
[regression module](../test_compiler_feedback_regressions.py) pins Git graph
and file-change proof, exact decimal ceilings, the evaluator resource limits,
receipt/diff integrity and SHA-256 repositories against the same frozen
contract. Run both without network, toolkit restoration or dependency installs:

```sh
python3 -B -m unittest -v test_compiler_feedback_contract test_compiler_feedback_regressions
```

The [contract workflow](../.github/workflows/compiler-feedback-contract.yml)
runs those same suites on pull requests, main and merge groups with read-only
permissions on a GitHub-hosted runner. It is separate from the shared pinned CI
producer inventory to avoid changing unrelated producers. Actions logs are
receipts of **acceptance test execution**, not evidence of real compiler repair,
product deployment or prevented edits. No live repair sample or production
outcome is claimed by these fixtures; real-work repair metrics remain unverified.

Undo this source-only slice with a reviewed revert. It installs no runtime,
monitor, hook or host automation and cannot undo a previously executed attempt.
Preserve external evidence and independently authored test provenance.
