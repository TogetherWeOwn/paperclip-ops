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

The initial local policy covers this checker, its workflow and its policy, mapped
to this page. It does not claim to cover all repository modules. Expand mappings
only where a real source-to-doc contract exists; avoid broad unrelated-doc bypasses.

## Reusable CI

`.github/workflows/docs-drift.yml` runs here on PRs, main pushes and merge groups.
External callers can use its `workflow_call` interface with `base-sha`, `head-sha`
and `tools-ref`. Pin both `uses: TogetherWeOwn/paperclip-ops/.github/workflows/docs-drift.yml@<full-sha>`
and `tools-ref` to the **same reviewed paperclip-ops commit**. The workflow checks
out the caller at head with complete history, and the checker separately at
`tools-ref`. For PR callers pass `github.event.pull_request.base.sha` and
`github.event.pull_request.head.sha`; other events must supply their own real
comparison SHAs. No arbitrary commands, generators or auto-fixes are executed.

Both checkouts disable persisted credentials; permissions are only `contents:
read`. Do not inherit secrets. No host actions, package installs, production
access or branch-protection edits are needed. The new job fails on findings/errors;
repositories choose separately whether to require that check for merging.
This extension does not edit or fork the separately staged Tier-0 foundation.
Rollback: revert the introducing PR, or remove the caller job/policy in a reviewed
change. Do not turn errors into successful empty-diff results.
