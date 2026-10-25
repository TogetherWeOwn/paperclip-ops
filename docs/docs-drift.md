# Documentation accompaniment gate

Use this opt-in, read-only check when specific code/config changes must include
an accompanying documentation change. It complements the generic pre-review docs
warning, generated-artifact checks and quality floor; it does not replace them.
It checks **accompaniment, not semantic correctness** or generated-file freshness.
A prose edit cannot prove that the docs accurately describe the implementation.

## Local check

Requires Python 3.10+ and Git, no installed Python packages or network:

```sh
python3 scripts/quality-gates/docs_drift.py --base <full-target-sha> --head <full-pr-head-sha>
python3 -m unittest -v test_docs_drift
```

The repository is unchanged; this check is its own dry run. Stdout is one JSON
object (`schema: docs-drift.v1`). Exit 0 is `clean` or explicitly
`not_configured`, exit 1 is `finding`, and exit 2 is `unmeasurable`.
Argument syntax errors use argparse's stderr/exit 2. Findings include a stable
`code`, rule ID, affected source paths, expected doc globs, `retryable: false`
and `next_action`. Unmeasurable Git timeouts are retryable; other errors require
the reported remediation. Git failures do not dump command output or credentials.
Git environment overrides and system/global configuration are ignored so they
cannot redirect `--repo` to a different repository.
The report carries immutable base/head/merge-base/policy SHAs and measured
`changed_paths` and `checked_rules`. Zero checked rules is not a coverage claim.

## Explain mode

Add `--explain` to cite which changed docs satisfy each triggered rule:

```sh
python3 scripts/quality-gates/docs_drift.py --base <full-target-sha> --head <full-pr-head-sha> --explain
```

Without `--explain`, stdout is exactly the `docs-drift.v1` object above:
same keys, values and exit codes. With it, a normal report (`clean`,
`finding` or `not_configured`) gains exactly one extra top-level key,
`explanations`; every other value and the exit code is identical to the
default invocation on the same base/head. `explanations` holds one record
per triggered (checked) rule, in base-policy rule order, each with exactly:

- `rule`: the existing stable rule ID (same spelling as `findings[].rule`);
- `matched_source_paths`: sorted, duplicate-free changed paths matching the
  rule's source patterns, including deleted and renamed-away source paths;
- `qualifying_doc_paths`: sorted, duplicate-free paths qualifying for this
  rule under the existing predicate — added or content-modified regular
  files matching the rule's doc patterns and not its source patterns;
- `status`: `satisfied` iff `qualifying_doc_paths` is non-empty, else
  `missing`.

`len(explanations) == checked_rules`. `missing` records correspond
one-for-one to `findings` (same rule, and `findings[].paths` equals
`matched_source_paths`); a `satisfied` rule never has a finding. Both come
from the same single evaluated path sets and predicate as the verdict, not a
second evaluator. Zero triggered rules yields `explanations: []` (never a
coverage claim); a missing base policy yields `not_configured` with
`explanations: []`. An `unmeasurable` refusal keeps exit 2 and invents no
satisfied evidence. Output carries only paths, rule IDs and the existing
commit identities — never file bodies or subprocess output. Explanations
prove accompaniment only, never semantic accuracy or generated-file
freshness.

Fetch complete common history before running. Missing objects, unrelated history,
multiple merge bases, malformed base policy and 30-second Git command timeouts
are errors, never clean passes. Each invocation allows at most 100000 changed
paths. The reusable job has a three-minute total timeout.

## Policy

Commit `.docs-drift.json` in the caller repository:

```json
{
  "version": 1,
  "rules": [
    {"id": "api", "sources": ["src/api/*"], "docs": ["docs/api.md"]}
  ]
}
```

The exact schema is shown above; unknown fields are errors. IDs are unique,
lowercase letters/digits/underscore/hyphen, at most 64 characters. At most 100
rules, 50 nonempty patterns per list, 256 characters per pattern and 64 KiB per
policy. Patterns are repository-relative and use Python `fnmatchcase`: matching
is case-sensitive, `*` includes slashes, and `**` has no extra meaning. No leading
slash, empty path segment, `.` or `..` segment, or NUL is allowed.

- Load policy from the **supplied target commit**, not PR files. A policy removed
  or weakened by a PR cannot exempt that same PR. A newly introduced policy only
  starts enforcing after merge; onboarding reports `not_configured` honestly.
- Compare changes from the unique merge base to the supplied head. Deleted
  source paths require docs too. Renames are delete+add, so old source paths
  remain in scope. Existing untouched legacy source does not trigger a rule.
- Each triggered rule needs at least one added or content-modified, regular
  tracked file matching its docs patterns **and not its source patterns**.
  A deleted doc, symlink, submodule or chmod-only change does not satisfy it.
  A renamed doc counts only when its new regular path matches the mapping.
- Git path lists are NUL-delimited; JSON escapes unusual paths (including newlines).
- Review policy expansions/relaxations normally. This is a development-quality
  check, not a security boundary against PRs that replace the checker/workflow.

The local policy has these exact-path contracts:

| Rule ID | Source paths | Required documentation |
|---|---|---|
| `docs-drift` | `scripts/quality-gates/docs_drift.py`, `.github/workflows/docs-drift.yml`, `.docs-drift.json` | `docs/docs-drift.md` |
| `capacity-evidence-contract` | `coolify_capacity_headroom.sh`, `coolify_capacity_headroom.py` | `docs/capacity-evidence.md` |
| `trace-correlation-contract` | `ops/paperclip/trace_correlator.py` | `docs/trace-correlation.md` |

Run the local check above with full target/head SHAs. New mappings enforce only
after merge, when present in the supplied target's policy. Accompaniment is not
semantic correctness, capacity admission, deployment authorization or evidence
freshness. Checking the trace source's documentation does not make the correlator
an execution/transport gate; its report and exit status must never admit or refuse
work. This policy does not cover all repository modules. Expand mappings only
where a real source-to-doc contract exists; avoid broad unrelated-doc bypasses.

## Reusable CI

`.github/workflows/docs-drift.yml` runs here on PRs, main pushes and merge groups.
The caller is checked out at the comparison head with complete history. The
checker is **always a separate checkout**: native events use `github.repository`
at `github.workflow_sha` (the immutable workflow-file revision), including in
forks or renamed copies. A PR head may predate the checker, or contain a different
checker; neither selects the executable. The workflow revision must contain the
checker. Missing tools fail the job, never fall back to caller files or skip.

Reusable callers, including this same repository, use its `workflow_call`
interface with `base-sha`, `head-sha` and `tools-ref`. Pin both
`uses: TogetherWeOwn/paperclip-ops/.github/workflows/docs-drift.yml@<full-sha>`
and `tools-ref` to the **same reviewed paperclip-ops commit**. Every reusable call
checks out the canonical paperclip-ops checker at `tools-ref`, not the caller's
workflow revision. Full lowercase SHAs, including native workflow provenance,
are validated before any checkout. Raw call inputs and native event SHAs are
validated too: empty or mutable values cannot fall back to different event
SHAs or tools. For PR callers pass `github.event.pull_request.base.sha` and
`github.event.pull_request.head.sha`; other events must supply their own real
comparison SHAs.

The checker still runs **in the caller directory**, so diffs and policy come from
caller history; the policy snapshot remains the supplied target/base commit,
not the tools checkout or PR policy. Stdout remains the single checker JSON
report. Stderr includes a `docs-drift.tools.v1` provenance object with exact
base/head, tools repository/ref and reusable-call mode. For hosted verification,
record the run/job URL, this provenance and the checker's policy/merge-base SHAs,
`changed_paths`, `checked_rules` and findings; a green job alone is not coverage.
Rerunning an old failed run uses that run's old workflow revision, not this repair;
verify on a genuine new native event instead. No arbitrary commands, generators
or auto-fixes are executed.

Both checkouts disable persisted credentials; permissions are only `contents:
read`. Do not inherit secrets. No host actions, package installs, production
access or branch-protection edits are needed. The new job fails on findings/errors;
repositories choose separately whether to require that check for merging.
This extension does not edit or fork the separately staged Tier-0 foundation.
Rollback: revert the introducing PR, or remove the caller job/policy in a reviewed
change. Do not turn errors into successful empty-diff results.
