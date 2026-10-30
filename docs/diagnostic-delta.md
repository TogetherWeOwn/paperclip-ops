# Diagnostic identity delta ratchet

Use this opt-in, read-only check when an `ax-lint` diagnostic baseline must not
regress: it compares the diagnostics at two exact snapshots and reports
net-new in-scope identities. It measures **identity delta, not semantic
correctness**. A clean report cannot prove that the scanned scripts implement
their advertised flags, and the scanner is a bounded heuristic, not a full
YAML/shell parser. No scanned code or generators execute during measurement.

## Local check

The checker requires Python 3.10+, Git 2.29+ and POSIX process groups (the hosted
workflow uses Linux), with no installed Python packages or network. It refuses
all Git transport protocols, including on versions predating `GIT_NO_LAZY_FETCH`;
missing objects in a partial clone are errors, never hydrated during measurement.
`--repo` may name any existing subdirectory of the checkout: policy lookup and
snapshot enumeration always use the full repository tree.
The offline workflow tests additionally need PyYAML; use the repository
[verification environment](../README.md#verification). The old-Git fixture shim
resolves an absolute system Git binary before changing `PATH`, refuses recursive
entry, and gives its direct probes three-second deadlines. It does not delegate
through an inherited Git launcher or use credentials for fixture repositories.

```sh
python3 scripts/quality-gates/diagnostic_delta.py --repo <tree> --base <full-base-sha> --head <full-head-sha>
python3 -m unittest -v test_diagnostic_delta test_diagnostic_delta_workflow test_diagnostic_delta_hardening
```

The repository is unchanged; this check is its own dry run. Stdout is one JSON
object. Exit 0 is `clean` or explicitly `not_configured`, exit 1 is `finding`,
and exit 2 is `unmeasurable`. A repository whose base snapshot carries no
`.diagnostic-delta.json` policy reports `not_configured` with
`enforced: false`: that is onboarding state, explicitly **not** evidence of
enforcement. Configured reports use `enforced: true` and carry immutable
base/head/policy SHAs, occurrence counts (`before`, `after`, `new`,
`resolved`), sorted `new`/`resolved` identity lists, and per-snapshot coverage
(`skipped`, `unknown` for base and head). Counts refer to in-scope
occurrences. Errors name a nonempty `code`/`next_action` pair and a boolean
`retryable`. Git environment overrides and system/global configuration are
ignored so they cannot redirect `--repo` to a different repository. Supplied
SHAs must match the repository's object format (40 hex for SHA-1, 64 for
SHA-256) and be the exact full object ID they resolve to: refs, hex-looking
branch names and abbreviations are refused before any snapshot is read.

## Identity and comparison rules

An identity is `{rule_id, path, subject}`:

- `subject` is `"file"` for `agents-map/*` and
  `script-surface/no-json-or-dry-run` findings.
- `subject` is `"input:<exact-name>"` for `dispatch-inputs/*` findings, where
  the name comes from the supported evidence key, never from remediation
  prose.
- `path` is a canonical relative POSIX path: absolute paths, `.`/`..`/empty
  segments, backslashes and case folding are rejected, never normalised away.

Comparison ignores report root, line shifts, finding order, whitespace and
comments on an input entry, and message/remediation text. There is **no rename
credit**: even a byte-identical Git rename is one `resolved` plus one `new`
identity. Duplicate identities are refused as `unmeasurable`, never
set-deduped or given unstable ordinals; the check deliberately cannot
distinguish multiple file-subject occurrences at one path.

## Policy

Commit `.diagnostic-delta.json` in the caller repository. It is a strict JSON
object:

```json
{
  "version": 1,
  "producer": "ax-lint",
  "include": ["scripts/quality-gates/**"]
}
```

`version` must be `1`, `producer` must be `"ax-lint"`, and `include` is a list
of 1 to 64 repository-relative globs. `*`, `?` and character classes match within
one path component, never across `/`; a whole `**` component matches zero or
more directories. Thus `scripts/**/*.sh` covers both `scripts/run.sh` and
`scripts/sub/run.sh`, while `scripts/*.sh` covers only direct children. Unknown
fields, producers, versions, invalid paths and non-string globs fail closed.

- Load policy and scope from the **supplied base snapshot**, not from head.
  A policy removed or weakened by the change under measurement cannot exempt
  that same change; head's policy never alters the run. Base trust is the
  comparison scope, and scope can only be set by the already-merged baseline.
- Only recognised empty inline dispatch skips may remain explicit `skipped`
  coverage. Unsupported inputs and unknown or unreadable in-scope coverage are
  `unmeasurable`, even when unchanged between snapshots.
- The complete producer report, including coverage and summary, is validated;
  unknown rules, unsafe identities and oversized reports fail closed. The
  checker caps JSON reports and individual source files at 1 MiB, total scoped
  source at 32 MiB, scope at 10000 files, full trees at 100000 entries and each
  Git/producer subprocess at 30 seconds. A pinned AX-only collector drains
  producer pipes concurrently, collecting at most 1 MiB of stdout and 64 KiB of
  stderr; the first excess byte kills and reaps the producer, returning
  `producer_output_limit`. Git subprocess output is collected under the same
  discipline (per-call stdout limit, 64 KiB of stderr, kill and reap on the
  first excess byte), never spooled unbounded to temporary storage first. Both
  bounds are pinned constants in the checker: `GIT_TIMEOUT` (30 seconds) is the
  outer Git deadline and the collector's inner deadline is 25 seconds, leaving
  cleanup time within it. The collector announces its process group before
  starting the producer; the checker terminates that entire group on every
  return, including an outer timeout while the collector is stopped. Standalone
  collector calls retain their own producer-group cleanup. These collection bounds do not
  bound memory used internally by the trusted producer to construct its JSON.
  Policy size is at most 64 KiB and
  each glob at most 256 characters. Oversized inputs are unmeasurable, not clean.
- The producer scans a temporary projection containing only scoped regular
  UTF-8 files. Scoped symlinks/submodules, their possible ancestors and the
  producer's excluded directories are refused, rather than silently followed
  or certified. Possible ancestors are judged against the complete path
  components before a pattern's first wildcard component, so replacing
  `scripts/pkg-one` with a symlink or gitlink under a `scripts/pkg-*/**`
  scope is refused, not reported as resolved debt. Quoted
  `"on"` / `"workflow_dispatch"` / `"inputs"` container keys are recognised
  dispatch containers, never silently cleared. Its ordinary test-file and non-CLI
  exclusions still apply.

The initial policy covers only this checker's own directory
(`scripts/quality-gates/**`). It does not claim to cover all repository
modules. Expand coverage only where a real diagnostic contract exists.

## Reusable CI

`.github/workflows/diagnostic-delta.yml` exposes a `workflow_call` interface
with required `base-sha`/`head-sha` inputs (full lowercase 40-character SHA-1
commit SHAs) and a `tools-ref` input (a full lowercase 40-character commit SHA of
this SHA-1 tooling repository), plus an
optional `artifact-name` input (default
`diagnostic-delta-report`). It also exposes `workflow_dispatch` with the same inputs so a real
Actions run can produce evidence at exact SHAs. There is deliberately no push
trigger: native push events have no trusted caller-supplied base snapshot to
measure against. For dispatch runs, the provided base snapshot is the
documented trust contract for that run. Both checkout steps are SHA-1-only:
64-character caller SHAs are rejected before checkout. SHA-256 repositories
remain supported by the local checker, not by this hosted workflow.

Reusable callers pin both
`uses: TogetherWeOwn/paperclip-ops/.github/workflows/diagnostic-delta.yml@<full-sha>`
and `tools-ref` to the **same reviewed paperclip-ops commit**. The workflow
checks out the caller at head with complete history, and the checker
separately at `tools-ref`. Raw call inputs are validated before checkout;
empty or mutable inputs fail rather than falling back to event SHAs or local
tools. The hosted job runs the pinned checker only, never caller-supplied
scripts: a caller-local checker copy is ignored even when present. The JSON
report is written to a workspace-specific absolute path (never a shared
machine-global file), retained with `always()` even
on failure, and a missing report is an error, never a silent pass. The
checker's nonzero exit status is preserved through the report capture.
Artifacts are never overwritten: a caller that invokes the workflow more
than once in a single workflow run (including through a matrix) must pass a
distinct `artifact-name` for each invocation, or the later upload fails
instead of replacing the earlier comparison's evidence.

Both checkouts disable persisted credentials; permissions are only `contents:
read`. Do not inherit secrets. No host actions, package installs, production
access or branch-protection edits are needed. The new job fails on
findings/errors; repositories choose separately whether to require that check
for merging. This extension does not edit or fork the separately staged Tier-0
foundation. Rollback: revert the introducing change, or remove the caller
job/policy in a reviewed change. Do not turn errors into successful
empty-delta results.

## Fixture versus live evidence

Be precise about what each layer proves. The `unittest` suites run offline
against fixture repositories and a fixture executable that records its
arguments: they prove SHA routing, argument quoting, retained report bytes and
nonzero propagation, plus the workflow's structural boundary (permissions,
runner, pinned actions, checkout layout). They do **not** claim a hosted
Actions run occurred, and the fixture executable does not implement the
ratchet or fake its verdict. Live enforcement evidence needs a real Actions
run of the reusable workflow at exact SHAs, with its retained JSON report
artifact. Reports also include `checker_sha256`, `collector_sha256` and
`producer_sha256` of the actual executing files (null if unreadable). Cross-check these against the
reviewed `tools-ref`, and retain the run's immutable workflow/input provenance:
a JSON verdict alone is not proof that an approved checker was selected. The
workflow validates that the refs are immutable, not that a caller selected the
same approved revision for its workflow and tools; that pin alignment remains
the caller's reviewed configuration contract.
