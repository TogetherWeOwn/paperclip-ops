# Review bake-off hygiene

The harness can score saved results without making model calls. This change does
not authorize any new model comparisons or spend. A future comparison still
requires an explicit owner request and an upfront cost estimate.

## Pinned effort

`run_review.py` pins `model_reasoning_effort` in the Codex invocation and writes
`effort` in every result. This is the **requested** effort, not an attestation that
a provider implemented it. There is no silent inference from filenames or model
defaults. `score.py` rejects missing/invalid effort, mixed efforts (including two
efforts for one model), invalid positive-integer repetition IDs, and duplicate
model/case/repetition cells before printing any scores. Score separate effort
cohorts in separate invocations; medium versus high is not a controlled model
comparison. Failed reviews remain in the recall denominator.

## Repeat spread before differences

Every score row includes requested effort, repetition count and **recall repeat
spread**, or the reason spread is unmeasured. The spread is the full max-minus-min
range of per-repetition defect recall, not the Wilson interval over pooled defect
counts. At least two repetitions per arm must have identical repetition IDs,
complete identical case sets and identical actual-prompt SHA-256 hashes for each
case across all arms/repetitions. A balanced subset of the case catalog is allowed.
`--rep` labels one batch; it does not silently schedule or purchase extra runs.
Both producer CLIs reject a nonpositive `--rep` with argument-error exit 2 before
reading cases, invoking a reviewer or creating result files. The default is 1.

The comparisons section prints a numeric recall gap only if its absolute value
is **strictly larger than the sum of both observed full ranges**. Otherwise the
difference is withheld. Other metrics in the score table remain descriptive;
the harness does not infer comparative wins from them. Observed zero spread with
two repeats is not proof of significance. This conservative threshold is not a
statistical significance test or a substitute for more evidence.

A singleton is unknown variance, never zero. Missing input hashes, incomplete
cohorts, selected retries and changed prompts cannot establish repeat spread.
The saved public reports contain one repetition per arm and no raw inputs/results;
there is no defensible spread estimate to recover from those tables. Their
previous comparative recommendations are withdrawn rather than inventing data.

## Graded-process boundary

The controller alone reads the case catalog, labels and Git worktree. It sends
only title/body, changed paths, diff and numbered changed-file context to Codex.
The exact inline prompt is hashed into `input_sha256`. No defect labels or other
arms' responses are passed as fields to the graded process.

Codex runs from a fresh private directory, not the repository worktree. Only the
public response schema is copied there. HOME and CODEX_HOME are fresh; the child
inherits only PATH, HOME, CODEX_HOME, a TMPDIR inside that private runtime and the
existing proxy key as OPENAI_API_KEY. Temporary files stay confined without
granting global `/tmp` access. The key is not put in argv or results. The Linux Landlock launcher restricts file
access for the CLI and every descendant: system executable/library directories
(`/usr`, `/bin`, `/lib`, `/lib64`) are read-only, the fresh runtime is writable,
and grader files, Git metadata, repository worktrees and other-arm result trees
outside those runtime roots are not readable or writable, even via symlinks.
Only specific public DNS/TLS bootstrap paths under `/etc` are readable; `/etc`
is not granted wholesale. Keep grader/data/result trees outside the system
runtime roots. Files already intentionally supplied in the inline diff/context
are review inputs, not secret grader state.

Codex, its interpreter and library chain must be installed under the granted
system roots (`/usr/local` is included in `/usr`); home-directory, `/opt` and Nix
installations are unsupported and fail closed. The controller always creates the
runtime with `mkdtemp`; the launcher is not a general-purpose sandbox for a
caller-supplied shared or system directory.

Landlock ABI >=3 on Linux x86_64 or aarch64 is mandatory. Unsupported platforms,
kernels, or blocked syscalls fail closed: **no unconfined retry**. This requires
no root privileges, user namespaces, new host scripts or host configuration.
The existing Codex read-only sandbox is retained as defense in depth; a prompt
asking not to use tools is not the security boundary. This is a filesystem
boundary, not network isolation or a guarantee about effective provider effort.

If the launcher exits or times out without a final message, the saved `errors`
include its stderr diagnostic, capped at 300 characters with the proxy key
redacted and terminal controls removed. Existing JSON-event errors and token
usage remain intact. Stderr handling is unchanged when a final message exists.

## Offline verification

```sh
python3 -m unittest discover -s eval/review-bakeoff -p 'test_*.py'
./test_case_61df0b37_review_bakeoff.sh
make verify-fast
```

The existing CI bake-off suite uses unittest discovery. Explicit offline hygiene,
launcher-contract and repetition-CLI producers also name their modules for the
suite-wiring guard; inventory changes can be inspected with
`python3 verification/offline_ci_contract.py --propose` before `--write`.
No live eval job is added. Tests exercise real confined child
and descendant processes, read/write/traversal/symlink denial, fail-closed
platform behavior, DNS/TLS read-only bootstrap, effort validation, balanced repeat
statistics, failed-run denominators, suppressed comparisons and mocked CLI
launch/input hashing. The launcher-contract suite executes the production CLI,
not just its helper, and checks the exact response-schema bytes and Git-directory
bypass at the mocked Codex launch. Removing any of those three prerequisites must
fail the tests. No model request is needed. An offline `codex --version` startup
check also uses no inference.
Existing historical dataset checks explicitly skip only the absent private bundle
in the fresh public snapshot; synthetic and confinement tests still run.

## Compatibility and rollback

`score_result` and legacy `summarize` counts remain available to the saved-result
trust audit. `summarize` adds effort/spread metadata, but the report CLI is strict.
The Markdown table now has named effort/spread/repetition columns plus a
comparisons footer; consumers must select columns by header, not fixed offsets
or the last output line. Old results without hashes may be described but cannot
support a measured gap. Revert the PR to roll back; no data migration, model pin,
production deployment or host change is involved.
